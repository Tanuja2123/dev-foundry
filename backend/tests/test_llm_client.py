"""LLM fallback client: per-provider timeouts/retries, the Groq model chain and skipping requests that cannot fit."""
import asyncio

import pytest
from agent_framework import Message
from agent_framework.openai import OpenAIChatCompletionClient

from app.services import llm_client as lc

pytestmark = pytest.mark.usefixtures("no_real_network")


class FakeProviderClient:
    def __init__(self, behaviour):
        self.behaviour, self.calls, self.streamed = behaviour, 0, []

    def get_response(self, stream=False, **kwargs):
        self.calls += 1
        self.streamed.append(stream)
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        if stream:
            final = self.behaviour

            class Stream:
                async def get_final_response(self):
                    return final
            return Stream()

        async def respond():
            return self.behaviour
        return respond()


def provider(name, behaviour, max_request_tokens=None, provider_id="groq"):
    return lc.ProviderSpec(provider_id=provider_id, name=name, client=FakeProviderClient(behaviour), model=name,
                           max_request_tokens=max_request_tokens)


def run(client, text="hello", **options):
    return asyncio.run(client.get_response([Message(role="user", contents=[text])], options=options))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(lc, "load_environment", lambda: None)
    for name in ("AZURE_OPENAI_APIKEY", "AZURE_OPENAI_ENDPOINT", "GROQ_API_KEY", "GROQ_MODEL", "MAQ_AI_API_KEY",
                 "GROQ_TIMEOUT_SECONDS", "GROQ_MAX_RETRIES", "MAQ_AI_TIMEOUT_SECONDS", "MAQ_AI_MAX_RETRIES",
                 "AZURE_OPENAI_TIMEOUT_SECONDS", "AZURE_OPENAI_MAX_RETRIES", "GROQ_MAX_REQUEST_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_every_provider_gets_explicit_timeout_and_retries(env):
    env.setenv("AZURE_OPENAI_APIKEY", "k")
    env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    env.setenv("GROQ_API_KEY", "k")
    env.setenv("MAQ_AI_API_KEY", "k")
    env.setenv("MAQ_AI_TIMEOUT_SECONDS", "45")
    client = lc.create_fallback_client()
    limits = {p.name: (p.client.client.timeout.read, p.client.client.max_retries) for p in client.providers}
    assert limits == {"Azure OpenAI": (120.0, 1), "Groq (gpt-oss-20b)": (60.0, 2), "Groq (gpt-oss-120b)": (60.0, 2),
                      "MAQ AI (SLM)": (45.0, 0)}
    assert [p.stream for p in client.providers] == [False, False, False, True]   # only MAQ AI is streamed
    assert all(p.client.client.timeout.connect == 10.0 for p in client.providers)


def test_groq_model_chain_from_env(env):
    env.setenv("GROQ_API_KEY", "k")
    env.setenv("GROQ_MODEL", "openai/gpt-oss-20b, qwen/qwen3.8-27b ,openai/gpt-oss-20b")
    env.setenv("GROQ_MAX_REQUEST_TOKENS", "6000")
    client = lc.create_fallback_client()
    assert [(p.model, p.max_request_tokens) for p in client.providers] == [
        ("openai/gpt-oss-20b", 6000), ("qwen/qwen3.8-27b", 6000)]


def test_request_too_large_for_groq_is_skipped_without_calling_it():
    small = provider("Groq (20b)", "groq-answer", max_request_tokens=8000)
    maq = provider("MAQ", "maq-answer", provider_id="maq_ai")
    client = lc.FallbackChatCompletionClient([small, maq])
    assert run(client, text="word " * 9000) == "maq-answer"
    assert small.client.calls == 0 and maq.client.calls == 1
    assert run(client, text="short question") == "groq-answer"


def test_instructions_and_tool_schemas_count_towards_the_request_size():
    from agent_framework import tool

    @tool(name="t", description="x " * 3000)
    def t(value: str) -> str:
        return value

    tokens = lc.estimate_request_tokens([Message(role="user", contents=["hi"])],
                                        {"instructions": "rules " * 2000, "tools": [t]})
    assert tokens > 4500


def test_rate_limited_groq_model_falls_through_to_the_next_groq_model():
    first = provider("Groq (20b)", RuntimeError("429 tokens per day exceeded"), max_request_tokens=8000)
    second = provider("Groq (120b)", "second-model", max_request_tokens=8000)
    client = lc.FallbackChatCompletionClient([first, second])
    assert run(client) == "second-model" and not first.is_healthy


def test_all_providers_skipped_or_failed_reports_each_reason():
    groq = provider("Groq (20b)", "never", max_request_tokens=100)
    maq = provider("MAQ", RuntimeError("524 timeout"), provider_id="maq_ai")
    with pytest.raises(RuntimeError) as info:
        run(lc.FallbackChatCompletionClient([groq, maq]), text="word " * 500)
    assert "skipped (request ~" in str(info.value) and "524 timeout" in str(info.value)


def test_apply_provider_limits_reads_overrides(env):
    env.setenv("GROQ_TIMEOUT_SECONDS", "30")
    env.setenv("GROQ_MAX_RETRIES", "0")
    client = OpenAIChatCompletionClient(model="m", api_key="k", base_url="https://api.groq.com/openai/v1")
    assert lc.apply_provider_limits(client, "groq") == (30.0, 0)
    assert client.client.timeout.read == 30.0 and client.client.max_retries == 0


def test_streamed_provider_returns_one_complete_response():
    maq = provider("MAQ", "complete-answer", provider_id="maq_ai")
    assert run(lc.FallbackChatCompletionClient([maq])) == "complete-answer" and maq.client.streamed == [True]


def test_streaming_can_be_disabled(env):
    env.setenv("MAQ_AI_STREAM", "false")
    assert provider("MAQ", "x", provider_id="maq_ai").stream is False


def test_provider_errors_are_summarised_for_the_logs():
    raw = RuntimeError("Error code: 429 - {'error': {'message': 'Rate limit reached for model `m` in organization "
                       "`org_secret` on tokens per minute (TPM): Limit 8000. Please try again in 1.6s. Need more tokens? "
                       "Upgrade', 'type': 'tokens'}}")
    summary = lc._short_error(raw)
    assert summary.startswith("HTTP 429: Rate limit reached") and "org_secret" not in summary and "Upgrade" not in summary
    assert lc._short_error(RuntimeError("Error code: 524 - {'title': 'cloudflare'}")).startswith("HTTP 524")


def test_users_see_a_short_message_and_logs_get_the_details(monkeypatch, caplog):
    from app.services import conversation_service as cs

    class Failing:
        current_model, current_provider_name = "m", "p"

    monkeypatch.setattr(cs, "get_agent_framework_client", lambda *a, **k: Failing())

    async def boom(*args, **kwargs):
        raise lc.AllProvidersFailedError(["[Groq (20b)]: skipped (request ~9500 tokens ...)",
                                          "[MAQ AI (SLM)] after 3.0s: HTTP 524: the gateway closed the request"])
    monkeypatch.setattr(cs.Agent, "run", boom)
    reply = asyncio.run(cs.generate_chat_response("hello", [], None))
    assert reply.startswith("⚠️ The AI service is busy or unavailable") and "524" not in reply and "Groq" not in reply
    reference = reply.split("Reference: ")[1].rstrip(")")
    assert any(reference in r.getMessage() and "HTTP 524" in r.getMessage() for r in caplog.records)
