import os
import json
import time
import asyncio
from typing import Any, Dict, List, Optional, Sequence, Mapping
from dotenv import load_dotenv

from agent_framework import Message
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework._types import ChatResponse, ResponseStream, ChatResponseUpdate
from app.core.logger import logger
from app.services import progress


# Per-provider HTTP limits. Without them the OpenAI SDK waits up to 600 s per attempt and retries twice, honouring a
# server "Retry-After" of up to 120 s, so a hanging provider (e.g. a Cloudflare 524 after ~100 s) could take ~9 minutes
# before the next provider is tried. Override with <PREFIX>_TIMEOUT_SECONDS / <PREFIX>_MAX_RETRIES in .env.
PROVIDER_LIMIT_DEFAULTS = {
    # provider_id: (env prefix, read timeout seconds, retries after the first attempt)
    "azure_openai": ("AZURE_OPENAI", 120.0, 1),
    # No SDK retries: a 429 (per-minute or daily quota) moves straight to the next model / key / provider and pauses
    # that model until its quota resets (see _rate_limit_wait), instead of waiting 17-34 s (seen live).
    "groq": ("GROQ", 60.0, 0),
    # The SLM can be slow: allow 10 minutes for the WHOLE answer (enforced by read_stream_with_watchdog, which also
    # gives up after STREAM_STALL_SECONDS without new text). Its gateway sits behind Cloudflare, which aborts a request
    # after 120 s without any response bytes (HTTP 524), so MAQ AI requests are streamed (see STREAMING_PROVIDERS).
    # No automatic retry: a retry would start over from scratch.
    "maq_ai": ("MAQ_AI", 600.0, 0),
}
STREAMING_PROVIDERS = {"maq_ai"}          # disable with MAQ_AI_STREAM=false
CONNECT_TIMEOUT_SECONDS = 10.0

# Groq's free tier allows 8,000 tokens per request (prompt + completion) and per minute, per model. Requests that
# cannot fit are skipped instead of being sent and rejected with HTTP 413. Override with GROQ_MAX_REQUEST_TOKENS.
# Each model has its own free quota (8k tokens/min, 200k/day, 1k requests/day); qwen3.8-27b is the model MAQ AI runs.
DEFAULT_GROQ_MODELS = "openai/gpt-oss-20b,qwen/qwen3.8-27b,openai/gpt-oss-120b"
DEFAULT_GROQ_MAX_REQUEST_TOKENS = 8000
MIN_COMPLETION_TOKENS = 1000


class AllProvidersFailedError(RuntimeError):
    """Every configured LLM provider failed or was skipped. `errors` holds one technical line per provider (for logs)."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("All LLM providers failed: " + " | ".join(errors))


def _short_error(exc: Exception) -> str:
    """One-line, log-friendly summary of a provider error: HTTP status + the provider's own message (no response dump)."""
    import re
    text = " ".join(str(exc).split())
    code = re.search(r"Error code: (\d{3})", text)
    status = code.group(1) if code else ""
    if status == "524" or ("524" in text and "cloudflare" in text.lower()):
        return "HTTP 524: the gateway (Cloudflare) closed the request after 120 s without a response"
    if "Virtual Network/Firewall" in text:
        return "HTTP 403: access denied by the Azure OpenAI network/firewall rules (VPN / IP allow-list)"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in type(exc).__name__ or "timed out" in text:
        return f"timeout: no answer within the configured time ({type(exc).__name__})"
    message = re.search(r"""['"]message['"]: ['"](.+?)['"](?:, ['"]|\})""", text)
    detail = message.group(1) if message else text
    detail = re.sub(r" in organization `[^`]+`", "", detail)          # keep account ids out of the logs
    detail = re.sub(r"\s*Need more tokens\?.*$", "", detail)
    return (f"HTTP {status}: " if status else f"{type(exc).__name__}: ") + detail[:300]


def _env_bool(name: str, default: bool) -> bool:
    value = (os.getenv(name, "") or "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def apply_provider_limits(client: OpenAIChatCompletionClient, provider_id: str) -> tuple:
    """Sets the read timeout and retry count of the provider's underlying OpenAI SDK client. Returns (timeout, retries)."""
    prefix, default_timeout, default_retries = PROVIDER_LIMIT_DEFAULTS[provider_id]
    timeout = _env_float(f"{prefix}_TIMEOUT_SECONDS", default_timeout)
    retries = _env_int(f"{prefix}_MAX_RETRIES", default_retries)
    import httpx
    client.client = client.client.with_options(
        timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)), max_retries=retries)
    return timeout, retries


def _token_counter():
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("o200k_base")   # gpt-4.1 / gpt-oss tokenizer family
        return lambda text: len(encoding.encode(text, disallowed_special=()))
    except Exception:
        return lambda text: int(len(text) / 3.5) + 1


_count_tokens = _token_counter()


def estimate_request_tokens(messages: Sequence[Message], options: Optional[Mapping[str, Any]]) -> int:
    """Approximate prompt tokens of a chat request: messages + system instructions + tool and response schemas."""
    parts: List[str] = [m.text or "" for m in messages or []]
    options = options or {}
    parts.append(str(options.get("instructions") or ""))
    for item in options.get("tools") or []:
        spec = item.to_json_schema_spec() if hasattr(item, "to_json_schema_spec") else item
        parts.append(json.dumps(spec, default=str))
    response_format = options.get("response_format")
    if response_format is not None:
        schema = response_format.model_json_schema() if hasattr(response_format, "model_json_schema") else response_format
        parts.append(json.dumps(schema, default=str))
    return sum(_count_tokens(p) for p in parts) + 8 * len(parts)


def load_environment():
    """Reloads .env from repo root, backend folder, and current working directory."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root_env = os.path.normpath(os.path.join(current_dir, "../../../.env"))
    backend_env = os.path.normpath(os.path.join(current_dir, "../../.env"))

    if os.path.exists(repo_root_env):
        load_dotenv(dotenv_path=repo_root_env, override=True)
    if os.path.exists(backend_env):
        load_dotenv(dotenv_path=backend_env, override=True)
    load_dotenv(override=True)


# Quota pauses shared by every client in this process (clients are created per request): quota_key -> paused until
_QUOTA_PAUSED_UNTIL: Dict[str, float] = {}
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 60.0


def _rate_limit_wait(exc: Exception) -> Optional[float]:
    """Seconds until a rate-limited (HTTP 429) model can be used again; None if the error is not a rate limit."""
    import re
    text = " ".join(str(exc).split())
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if status != 429 and "429" not in text and "rate limit" not in text.lower():
        return None
    headers = getattr(response, "headers", None) or {}
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        value = headers.get(name) if hasattr(headers, "get") else None
        seconds = _duration_seconds(value) if value else None
        if seconds:
            return seconds
    match = re.search(r"try again in ((?:\d+h)?(?:\d+m)?[\d.]+s|\d+m|\d+h)", text)
    return (_duration_seconds(match.group(1)) if match else None) or DEFAULT_RATE_LIMIT_PAUSE_SECONDS


def _duration_seconds(value: str) -> Optional[float]:
    """'20m16.9s' / '7.2s' / '120' / '1h2m' -> seconds."""
    import re
    value = str(value).strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = re.findall(r"([\d.]+)(h|ms|m|s)", value)
    if not parts:
        return None
    factor = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(number) * factor[unit] for number, unit in parts)


def groq_api_keys() -> List[str]:
    """
    Groq API keys in order: GROQ_API_KEY (one key, or several separated by commas) followed by GROQ_API_KEY_2,
    GROQ_API_KEY_3, ... Each key is a separate Groq account with its own free quotas; when the first account's quota
    is used up, requests move on to the next account automatically. Duplicates are ignored.
    """
    keys = [k.strip() for k in (os.getenv("GROQ_API_KEY") or "").split(",") if k.strip()]
    for index in range(2, 10):
        keys += [k.strip() for k in (os.getenv(f"GROQ_API_KEY_{index}") or "").split(",") if k.strip()]
    return list(dict.fromkeys(keys))


def pause_seconds_left(quota_key: str) -> float:
    return max(0.0, _QUOTA_PAUSED_UNTIL.get(quota_key, 0.0) - time.time())


class ProviderSpec:
    """Represents a configured LLM provider in the fallback hierarchy."""

    def __init__(
        self,
        provider_id: str,
        name: str,
        client: OpenAIChatCompletionClient,
        model: str,
        base_url: Optional[str] = None,
        is_primary: bool = False,
        max_request_tokens: Optional[int] = None,
        quota_key: Optional[str] = None,
    ):
        self.provider_id = provider_id
        self.quota_key = quota_key or f"{provider_id}:{model}"     # one quota per account (API key) and model
        self.name = name
        self.client = client
        self.model = model
        self.base_url = base_url
        self.is_primary = is_primary
        self.max_request_tokens = max_request_tokens   # None = no per-request limit
        self.stream = provider_id in STREAMING_PROVIDERS and _env_bool(
            f"{PROVIDER_LIMIT_DEFAULTS.get(provider_id, ('X',))[0]}_STREAM", True)
        self.is_healthy = True
        self.last_failure_time: float = 0.0
        self.last_error: Optional[str] = None

    def mark_failed(self, error: Exception):
        self.is_healthy = False
        self.last_failure_time = time.time()
        self.last_error = str(error)

    def mark_success(self):
        self.is_healthy = True
        self.last_error = None


class FallbackChatCompletionClient(OpenAIChatCompletionClient):
    """
    Tiered Fallback Chat Completion Client for Microsoft Agent Framework.

    Fallback Hierarchy:
    1. Primary: Azure OpenAI (or OpenAI)
    2. Secondary: Groq, one provider per configured model (default openai/gpt-oss-20b, then openai/gpt-oss-120b).
       Groq's free-tier quotas are per model, so a model that hit its daily limit falls through to the next one.
    3. Tertiary: MAQ AI Gateway / SLM (OpenAI-compatible internal gateway, e.g. qwen-3.8-27b)
    Each provider has its own timeout / retry limits (PROVIDER_LIMIT_DEFAULTS); providers with a per-request token
    limit (Groq) are skipped for requests that cannot fit.
    """

    COOLDOWN_SECONDS = 180  # After 3 minutes, give an unhealthy provider another chance
    FIREWALL_COOLDOWN_SECONDS = 1800  # 30 minutes cooldown for firewall/network blocked providers (e.g. 403)

    def __init__(self, providers: List[ProviderSpec]):
        if not providers:
            raise ValueError(
                "No LLM providers are configured. Please set at least one of:\n"
                "- AZURE_OPENAI_APIKEY & AZURE_OPENAI_ENDPOINT\n"
                "- GROQ_API_KEY (with GROQ_MODEL, e.g. openai/gpt-oss-20b,openai/gpt-oss-120b)\n"
                "- MAQ_AI_API_KEY (with MAQ_AI_BASE_URL, e.g. https://llm.maqsoftware.net/v1)"
            )

        self.providers = providers
        self.active_provider_index = 0

        # Initialize the base class with the first provider's specifications
        first_provider = providers[0]
        super().__init__(
            model=first_provider.model,
            api_key="dummy-key-fallback",
            base_url="http://localhost/v1",
        )
        self.model = first_provider.model

    @property
    def current_provider(self) -> ProviderSpec:
        if 0 <= self.active_provider_index < len(self.providers):
            return self.providers[self.active_provider_index]
        return self.providers[0]

    @property
    def current_provider_name(self) -> str:
        return self.current_provider.name

    @property
    def current_model(self) -> str:
        return self.current_provider.model

    def _get_candidate_provider_indices(self) -> List[int]:
        """Returns provider indices in priority order, prioritizing healthy ones."""
        now = time.time()
        healthy_indices = []
        cooldown_indices = []

        for idx, provider in enumerate(self.providers):
            cooldown = (
                self.FIREWALL_COOLDOWN_SECONDS
                if (provider.last_error and ("403" in provider.last_error or "Virtual Network" in provider.last_error))
                else self.COOLDOWN_SECONDS
            )
            if provider.is_healthy:
                healthy_indices.append(idx)
            elif (now - provider.last_failure_time) > cooldown:
                cooldown_indices.append(idx)

        # If any providers are healthy or out of cooldown, try them in hierarchy order
        candidates = healthy_indices + cooldown_indices
        if not candidates:
            # If all are marked failed within cooldown, reset and try all in priority order
            candidates = list(range(len(self.providers)))

        return candidates

    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool = False,
        options: Optional[Mapping[str, Any]] = None,
        compaction_strategy: Any = None,
        tokenizer: Any = None,
        function_invocation_kwargs: Optional[Mapping[str, Any]] = None,
        client_kwargs: Optional[Mapping[str, Any]] = None,
        middleware: Any = None,
    ):
        """Dispatches the chat completion call to the active provider with automatic failover."""

        async def _execute_with_fallback():
            candidate_indices = self._get_candidate_provider_indices()
            errors = []
            request_tokens = None

            for idx in candidate_indices:
                provider = self.providers[idx]
                paused = pause_seconds_left(provider.quota_key)
                if paused:
                    errors.append(f"[{provider.name}]: skipped (quota exhausted, resets in {paused:.0f}s)")
                    logger.info(f"[LLM Fallback] Skipping '{provider.name}': quota exhausted, resets in {paused:.0f}s.")
                    groq = [p for p in self.providers if p.provider_id == "groq"]
                    if provider.provider_id == "groq" and groq and all(pause_seconds_left(p.quota_key) for p in groq):
                        soonest = min(pause_seconds_left(p.quota_key) for p in groq)
                        logger.warning(f"[LLM Quota] Every Groq key/model is out of quota (first one back in "
                                       f"{soonest / 60:.0f} min). Requests use MAQ AI meanwhile; add another key as "
                                       "GROQ_API_KEY_2 in .env for more Groq capacity.")
                    continue
                provider_messages = messages
                provider_tokens = None
                if provider.max_request_tokens:
                    if request_tokens is None:
                        request_tokens = estimate_request_tokens(messages, options)
                    provider_tokens = request_tokens
                    if request_tokens + MIN_COMPLETION_TOKENS > provider.max_request_tokens:
                        # Small per-request limit (Groq): send the newest part of the conversation that fits
                        trimmed = trim_to_request_limit(messages, options, provider.max_request_tokens)
                        if trimmed is None:
                            reason = (f"request ~{request_tokens} tokens + {MIN_COMPLETION_TOKENS} for the answer exceeds "
                                      f"its {provider.max_request_tokens}-token request limit even without history")
                            errors.append(f"[{provider.name}]: skipped ({reason})")
                            logger.info(f"[LLM Fallback] Skipping '{provider.name}': {reason}.")
                            continue
                        provider_messages = trimmed
                        provider_tokens = estimate_request_tokens(trimmed, options)
                        logger.info(f"[LLM Fallback] '{provider.name}': kept the newest {len(trimmed)} of "
                                    f"{len(messages)} messages (~{provider_tokens} tokens) to fit its request limit")
                effective_options = dict(options) if options is not None else {}
                effective_options["model"] = provider.model

                # Protect TPM limits: Groq on-demand counts prompt_tokens + max_tokens against its per-request limit
                # (8,000 on the free tier). Give the answer all the room that is left; other providers are not capped.
                if provider.max_request_tokens:
                    safe_max = max(MIN_COMPLETION_TOKENS, provider.max_request_tokens - provider_tokens - 100)
                    req_max = effective_options.get("max_tokens") or effective_options.get("max_completion_tokens") or safe_max
                    effective_options["max_tokens"] = min(req_max, safe_max)

                # Stream internally (keeps slow gateways alive) but still return one complete response
                stream_internally = provider.stream and not stream
                logger.info(
                    f"[LLM Dispatch] Routing request to '{provider.name}' (Model: {provider.model}, "
                    f"MaxTokens: {effective_options.get('max_tokens')}{', streamed' if stream_internally else ''})..."
                )
                started = time.time()
                progress.detail(backup_notice(provider, errors))

                async def call_provider() -> Any:
                    res = provider.client.get_response(
                        messages=provider_messages,
                        stream=stream or stream_internally,
                        options=effective_options,
                        compaction_strategy=compaction_strategy,
                        tokenizer=tokenizer,
                        function_invocation_kwargs=function_invocation_kwargs,
                        client_kwargs=client_kwargs,
                        middleware=middleware,
                    )
                    if asyncio.iscoroutine(res):
                        res = await res
                    if stream_internally:
                        res = await read_stream_with_watchdog(res, provider)
                    return res

                try:
                    try:
                        res = await call_provider()
                    except StreamStalledError as stalled:
                        if stalled.updates != 0:
                            raise
                        # Not a single token before the stall limit: a cold start or a queue at the gateway, not a
                        # stuck answer - try the same provider once more before giving up (ISSUES AGT-46, ref eeabebc5)
                        logger.warning(f"[LLM Fallback] '{provider.name}' sent nothing for {stream_stall_seconds():.0f}s; "
                                       "trying it once more")
                        progress.detail(f"{provider.name} is slow to start; trying it once more")
                        res = await call_provider()
                    if is_empty_response(res):
                        raise RuntimeError("empty response (no text and no tool call)")
                    logger.info(f"[LLM Dispatch] '{provider.name}' answered in {time.time() - started:.1f}s")

                    provider.mark_success()
                    self.active_provider_index = idx
                    self.model = provider.model
                    return res

                except Exception as exc:
                    err_summary = _short_error(exc)
                    provider.mark_failed(exc)
                    wait = _rate_limit_wait(exc)
                    if wait:
                        _QUOTA_PAUSED_UNTIL[provider.quota_key] = time.time() + wait
                        logger.warning(f"[LLM Fallback] '{provider.name}' rate limited: paused for {wait:.0f}s, "
                                       "moving on without waiting")
                    errors.append(f"[{provider.name}] after {time.time() - started:.1f}s: {err_summary}")
                    logger.warning(
                        f"[LLM Fallback] Provider '{provider.name}' failed after {time.time() - started:.1f}s "
                        f"({err_summary}). Attempting next provider in fallback chain..."
                    )
                    logger.debug(f"[LLM Fallback] Full error from '{provider.name}': {exc!r}")

            # If all candidate providers failed
            logger.error("[LLM Fallback] All LLM providers exhausted:\n  " + "\n  ".join(errors))
            raise AllProvidersFailedError(errors)

        if stream:
            # Non-streaming is standard across all agents in DevFoundry, but handle stream if requested
            return _execute_with_fallback()
        return _execute_with_fallback()


def create_fallback_client(order: Optional[List[str]] = None) -> FallbackChatCompletionClient:
    """Builds the configured FallbackChatCompletionClient according to available environment variables.
    `order` overrides the provider order (default: LLM_PROVIDER_ORDER)."""
    load_environment()
    providers: List[ProviderSpec] = []

    # 1. Primary: Azure OpenAI
    azure_key = os.getenv("AZURE_OPENAI_APIKEY", "").strip()
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    azure_version = os.getenv("AZURE_OPENAI_VERSION", "2024-12-01-preview").strip()
    azure_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1").strip() or "gpt-4"

    if azure_key and azure_endpoint:
        try:
            client = OpenAIChatCompletionClient(
                model=azure_deployment,
                api_key=azure_key,
                azure_endpoint=azure_endpoint,
                api_version=azure_version,
            )
            timeout, retries = apply_provider_limits(client, "azure_openai")
            providers.append(
                ProviderSpec(
                    provider_id="azure_openai",
                    name="Azure OpenAI",
                    client=client,
                    model=azure_deployment,
                    base_url=azure_endpoint,
                    is_primary=True,
                )
            )
            logger.info(f"[LLM Config] Configured provider: Azure OpenAI ({azure_deployment}; "
                        f"timeout {timeout:g}s, retries {retries})")
        except Exception as e:
            logger.warning(f"[LLM Config] Failed to initialize Azure OpenAI client: {e}")

    # 2. Groq: every API key (= one Groq account, own quotas) x every model (own quota per model), tried in order
    groq_base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
    groq_models = [m.strip() for m in (os.getenv("GROQ_MODEL", "") or DEFAULT_GROQ_MODELS).split(",") if m.strip()]
    groq_max_tokens = _env_int("GROQ_MAX_REQUEST_TOKENS", DEFAULT_GROQ_MAX_REQUEST_TOKENS) or None

    for key_index, groq_key in enumerate(groq_api_keys()):
        account = "" if key_index == 0 else f"#{key_index + 1} "
        for groq_model in dict.fromkeys(groq_models):
            try:
                client = OpenAIChatCompletionClient(
                    model=groq_model,
                    api_key=groq_key,
                    base_url=groq_base_url,
                )
                timeout, retries = apply_provider_limits(client, "groq")
                providers.append(
                    ProviderSpec(
                        provider_id="groq",
                        name=f"Groq {account}({groq_model.split('/')[-1]})",
                        client=client,
                        model=groq_model,
                        base_url=groq_base_url,
                        max_request_tokens=groq_max_tokens,
                        quota_key=f"groq:{key_index}:{groq_model}",
                    )
                )
                logger.info(f"[LLM Config] Configured provider: Groq {account}({groq_model}; timeout {timeout:g}s, "
                            f"retries {retries}, max {groq_max_tokens} tokens per request)")
            except Exception as e:
                logger.warning(f"[LLM Config] Failed to initialize Groq client ({groq_model}): {e}")

    # 3. Tertiary: MAQ AI Gateway / SLM
    maq_key = os.getenv("MAQ_AI_API_KEY", "").strip()
    maq_base_url = os.getenv("MAQ_AI_BASE_URL", "https://indiaai.maqsoftware.net/v1").strip()
    maq_model = os.getenv("MAQ_AI_MODEL", "qwen-3.8-27b").strip() or "qwen-3.8-27b"

    if maq_key:
        try:
            client = OpenAIChatCompletionClient(
                model=maq_model,
                api_key=maq_key,
                base_url=maq_base_url,
            )
            timeout, retries = apply_provider_limits(client, "maq_ai")
            providers.append(
                ProviderSpec(
                    provider_id="maq_ai",
                    name="MAQ AI (SLM)",
                    client=client,
                    model=maq_model,
                    base_url=maq_base_url,
                )
            )
            logger.info(f"[LLM Config] Configured provider: MAQ AI ({maq_model}; timeout {timeout:g}s, "
                        f"retries {retries}, streamed: {providers[-1].stream})")
        except Exception as e:
            logger.warning(f"[LLM Config] Failed to initialize MAQ AI client: {e}")

    if not providers:
        logger.error(
            "[LLM Config] No valid API keys found for Azure OpenAI, Groq, or MAQ AI. "
            "Please check .env configuration."
        )

    # Fallback order: Azure OpenAI -> MAQ AI -> Groq by default. MAQ AI (131k-token context, no daily quota) takes the
    # load when Azure is unavailable; Groq (8k tokens per request, 200k per day on the free tier) is the last resort.
    order = order or provider_order()
    providers.sort(key=lambda p: order.index(p.provider_id) if p.provider_id in order else len(order))
    logger.info("[LLM Config] Fallback order: " + " -> ".join(p.name for p in providers))
    return FallbackChatCompletionClient(providers)


DEFAULT_PROVIDER_ORDER = "azure_openai,maq_ai,groq"
# The Orchestrator makes one small structured routing decision per message: Groq answers it in 1-3 s, while the busy
# MAQ AI gateway took up to ~3 minutes (seen live). Heavy agents keep LLM_PROVIDER_ORDER.
DEFAULT_ORCHESTRATOR_PROVIDER_ORDER = "azure_openai,groq,maq_ai"


def provider_order() -> List[str]:
    """LLM_PROVIDER_ORDER: comma-separated provider ids (azure_openai, maq_ai, groq) in fallback order."""
    raw = os.getenv("LLM_PROVIDER_ORDER") or DEFAULT_PROVIDER_ORDER
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


DEFAULT_STREAM_STALL_SECONDS = 120.0


class StreamStalledError(RuntimeError):
    """A streamed answer stopped producing text (or exceeded the provider's total time)."""

    def __init__(self, message: str, updates: Optional[int] = None):
        super().__init__(message)
        self.updates = updates          # how many updates arrived before it stopped (0 = not a single token)


def stream_stall_seconds() -> float:
    """STREAM_STALL_SECONDS: a streamed answer with no new text for this long is abandoned (default 120)."""
    return max(10.0, _env_float("STREAM_STALL_SECONDS", DEFAULT_STREAM_STALL_SECONDS))


def provider_total_seconds(provider_id: str) -> float:
    """The whole answer must arrive within the provider's timeout (<PREFIX>_TIMEOUT_SECONDS), not just each chunk."""
    prefix, default_timeout, _ = PROVIDER_LIMIT_DEFAULTS.get(provider_id, ("X", 900.0, 0))
    return _env_float(f"{prefix}_TIMEOUT_SECONDS", default_timeout)


async def read_stream_with_watchdog(stream: Any, provider: Any) -> Any:
    """
    Reads a streamed answer to the end with two limits (seen live: a MAQ AI answer streamed for 27 minutes, because the
    HTTP read timeout only covers the gap between chunks): no new update for stream_stall_seconds(), and the whole
    answer within the provider's total time. Either one closes the stream and raises StreamStalledError, so the
    fallback chain moves on and the user is told instead of waiting.
    """
    stall, total = stream_stall_seconds(), provider_total_seconds(getattr(provider, "provider_id", ""))
    started = time.time()
    updates = 0
    if not hasattr(stream, "__aiter__"):                 # not iterable update by update: only the total deadline
        try:
            return await asyncio.wait_for(stream.get_final_response(), timeout=total)
        except asyncio.TimeoutError:
            raise StreamStalledError(f"no complete answer within {total:.0f}s")
    iterator = stream.__aiter__()
    # Time while one of our tools runs (e.g. the generator commands inside the Repo Setup agent's turn) is not the
    # model being silent: it counts neither as a stall nor toward the total (seen live: 166 s of generators was taken
    # for a stalled model, the turn failed and the structure was saved after the reply)
    tool_seconds, idle = 0.0, 0.0
    step = max(0.05, min(5.0, stall / 4))
    next_update: Optional[asyncio.Future] = None
    try:
        while True:
            if time.time() - started - tool_seconds >= total:
                raise StreamStalledError(f"no complete answer within {total:.0f}s")
            if next_update is None:
                next_update = asyncio.ensure_future(iterator.__anext__())
            slice_start = time.time()
            done, _ = await asyncio.wait({next_update}, timeout=step)
            waited = time.time() - slice_start
            if not done:
                if progress.tools_running():
                    tool_seconds += waited
                    continue
                idle += waited
                if idle >= stall:
                    raise StreamStalledError(f"the stream stalled: no new text for {stall:.0f}s "
                                             f"(after {time.time() - started:.0f}s, {updates} updates)", updates)
                continue
            try:
                next_update.result()
            except StopAsyncIteration:
                next_update = None
                break
            next_update, idle = None, 0.0
            updates += 1
            if updates % 50 == 0:
                progress.detail(f"{getattr(provider, 'name', 'The model')} is writing… "
                                f"({time.time() - started:.0f}s)")
    except StreamStalledError:
        if next_update is not None and not next_update.done():
            next_update.cancel()
        await _close_quietly(stream)
        raise
    return await stream.get_final_response()


async def _close_quietly(stream: Any) -> None:
    try:
        await stream.close()
    except Exception:
        pass


def backup_notice(provider: Any, errors: List[str]) -> str:
    """
    The waiting bubble's detail line (FEATURES REV-5): which model is answering, and - once an earlier one failed or was
    skipped - that a backup took over and how long it may take (otherwise a slow fallback looks like a hang).
    """
    name = getattr(provider, "name", "the model")
    if not errors:
        return f"Using {name}"
    slow = getattr(provider, "stream", False)                 # the streamed tier (MAQ AI) takes minutes for documents
    return (f"The main model is busy, so a backup ({name}) is answering"
            + ("; this can take a few minutes" if slow else ""))


def is_empty_response(response: Any) -> bool:
    """True for a response with no text and no tool call (seen live from MAQ AI: an empty TRD answer)."""
    messages = getattr(response, "messages", None)
    if messages is None:
        return False
    for message in messages:
        for content in getattr(message, "contents", None) or []:
            kind = str(getattr(content, "type", "") or "")
            if kind.startswith("function") or kind in ("tool_call", "tool_result"):
                return False
            if kind == "text" and (getattr(content, "text", "") or "").strip():
                return False
    return True


def orchestrator_provider_order() -> List[str]:
    """ORCHESTRATOR_PROVIDER_ORDER: provider order for the Orchestrator's routing decision only."""
    raw = os.getenv("ORCHESTRATOR_PROVIDER_ORDER") or DEFAULT_ORCHESTRATOR_PROVIDER_ORDER
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def trim_to_request_limit(messages: Sequence[Message], options: Optional[Mapping[str, Any]],
                          limit: int) -> Optional[List[Message]]:
    """
    The newest messages that fit `limit` tokens (with room for the answer), for providers with a small per-request
    limit (Groq). The oldest messages go first; the last message (the current request with its workflow context) is
    always kept. A tool result whose call was dropped is dropped too. None when even the last message does not fit.
    """
    kept = list(messages or [])
    while len(kept) > 1 and estimate_request_tokens(kept, options) + MIN_COMPLETION_TOKENS > limit:
        kept.pop(0)
        while len(kept) > 1 and str(getattr(kept[0], "role", "")) in ("tool", "Role.TOOL"):
            kept.pop(0)
    if estimate_request_tokens(kept, options) + MIN_COMPLETION_TOKENS > limit:
        return None
    return kept
