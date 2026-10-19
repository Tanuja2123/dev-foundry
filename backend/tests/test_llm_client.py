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
    # Groq: no SDK retries - a 429 moves straight on to the next model / provider (no 17-34 s waits)
    assert limits == {"Azure OpenAI": (120.0, 1), "Groq (gpt-oss-20b)": (60.0, 0), "Groq (qwen3.8-27b)": (60.0, 0),
                      "Groq (gpt-oss-120b)": (60.0, 0), "MAQ AI (SLM)": (45.0, 0)}
    # default order: Azure OpenAI -> MAQ AI -> Groq (MAQ AI takes the load; Groq's daily quota is the last resort)
    assert [p.name for p in client.providers] == ["Azure OpenAI", "MAQ AI (SLM)", "Groq (gpt-oss-20b)",
                                                  "Groq (qwen3.8-27b)", "Groq (gpt-oss-120b)"]
    assert [p.stream for p in client.providers] == [False, True, False, False, False]   # only MAQ AI is streamed
    assert all(p.client.client.timeout.connect == 10.0 for p in client.providers)


def test_provider_order_is_configurable(env):
    env.setenv("AZURE_OPENAI_APIKEY", "k")
    env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    env.setenv("GROQ_API_KEY", "k")
    env.setenv("MAQ_AI_API_KEY", "k")
    env.setenv("LLM_PROVIDER_ORDER", "groq, azure_openai, maq_ai")
    client = lc.create_fallback_client()
    assert [p.provider_id for p in client.providers] == ["groq", "groq", "groq", "azure_openai", "maq_ai"]


def test_an_empty_answer_falls_back_to_the_next_provider():
    from types import SimpleNamespace
    from agent_framework import Message
    empty = SimpleNamespace(messages=[Message(role="assistant", contents=[""])])
    full = SimpleNamespace(messages=[Message(role="assistant", contents=["the TRD"])])
    assert lc.is_empty_response(empty) and not lc.is_empty_response(full)
    assert not lc.is_empty_response("plain string")
    first = provider("MAQ", empty, provider_id="maq_ai")
    second = provider("Groq", full)
    client = lc.FallbackChatCompletionClient([first, second])
    asyncio.run(client.get_response([Message(role="user", contents=["hi"])]))
    assert first.client.calls == 1 and second.client.calls == 1


def test_orchestrator_has_its_own_fast_provider_order(env):
    env.setenv("AZURE_OPENAI_APIKEY", "k")
    env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    env.setenv("GROQ_API_KEY", "k")
    env.setenv("MAQ_AI_API_KEY", "k")
    assert lc.orchestrator_provider_order() == ["azure_openai", "groq", "maq_ai"]
    client = lc.create_fallback_client(order=lc.orchestrator_provider_order())
    assert [p.provider_id for p in client.providers] == ["azure_openai", "groq", "groq", "groq", "maq_ai"]
    assert [p.provider_id for p in lc.create_fallback_client().providers] == ["azure_openai", "maq_ai", "groq", "groq", "groq"]
    env.setenv("ORCHESTRATOR_PROVIDER_ORDER", "maq_ai,groq")
    assert lc.orchestrator_provider_order() == ["maq_ai", "groq"]


def test_long_conversations_are_trimmed_for_groq_instead_of_skipping_it():
    from agent_framework import Message
    history = [Message(role="user" if i % 2 == 0 else "assistant", contents=["word " * 800]) for i in range(20)]
    latest = Message(role="user", contents=["the current request with its workflow context"])
    trimmed = lc.trim_to_request_limit(history + [latest], None, 8000)
    assert trimmed[-1] is latest and len(trimmed) < 21                  # newest kept, oldest dropped
    assert lc.estimate_request_tokens(trimmed, None) + lc.MIN_COMPLETION_TOKENS <= 8000
    assert lc.trim_to_request_limit([Message(role="user", contents=["word " * 9000])], None, 8000) is None

    groq = provider("Groq (20b)", "groq-answer", max_request_tokens=8000)
    client = lc.FallbackChatCompletionClient([groq])
    with pytest.raises(lc.AllProvidersFailedError):                     # one huge message alone: skipped
        run(client, text="word " * 9000)
    assert groq.client.calls == 0


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
    assert reply.startswith("⚠️ The AI service is busy or unavailable") and "HTTP 524" not in reply and "Groq" not in reply
    reference = reply.split("Reference: ")[1].rstrip(")")
    assert any(reference in r.getMessage() and "HTTP 524" in r.getMessage() for r in caplog.records)


def test_only_groq_gets_a_max_tokens_clamp_sized_to_the_room_left():
    seen = {}

    class Capturing(FakeProviderClient):
        def get_response(self, stream=False, options=None, **kwargs):
            seen[self.behaviour] = dict(options or {})
            return super().get_response(stream=stream, **kwargs)

    groq = lc.ProviderSpec("groq", "Groq", Capturing("groq"), "g", max_request_tokens=8000)
    azure = lc.ProviderSpec("azure_openai", "Azure", Capturing("azure"), "a")
    run(lc.FallbackChatCompletionClient([groq]), text="short question")
    run(lc.FallbackChatCompletionClient([azure]), text="short question")
    assert 6000 < seen["groq"]["max_tokens"] < 8000          # all the room left, not a fixed 1500
    assert "max_tokens" not in seen["azure"]                  # no cap: long PRD/TRD answers are not cut off


@pytest.fixture(autouse=True)
def _clear_quota_pauses():
    lc._QUOTA_PAUSED_UNTIL.clear()
    yield
    lc._QUOTA_PAUSED_UNTIL.clear()


def test_a_rate_limited_model_is_paused_and_skipped_without_waiting():
    class RateLimited(Exception):
        status_code = 429
    limited = provider("Groq (20b)", RateLimited("Error code: 429 - tokens per day. Please try again in 20m16.9s."),
                       max_request_tokens=8000)
    backup = provider("MAQ", "maq-answer", provider_id="maq_ai")
    client = lc.FallbackChatCompletionClient([limited, backup])
    assert run(client) == "maq-answer"
    assert 1200 < lc.pause_seconds_left(limited.quota_key) <= 1217
    assert run(client) == "maq-answer" and limited.client.calls == 1          # paused: not called again


def test_several_groq_keys_become_separate_accounts(env):
    env.setenv("GROQ_API_KEY", "key-a, key-b")
    env.setenv("GROQ_API_KEY_2", "key-c,key-a")
    env.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    assert lc.groq_api_keys() == ["key-a", "key-b", "key-c"]
    client = lc.create_fallback_client()
    assert [p.name for p in client.providers] == ["Groq (gpt-oss-20b)", "Groq #2 (gpt-oss-20b)", "Groq #3 (gpt-oss-20b)"]
    assert len({p.quota_key for p in client.providers}) == 3
