import os
import time
import asyncio
from typing import Any, Dict, List, Optional, Sequence, Mapping
from dotenv import load_dotenv

from agent_framework import Message
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework._types import ChatResponse, ResponseStream, ChatResponseUpdate
from app.core.logger import logger


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
    ):
        self.provider_id = provider_id
        self.name = name
        self.client = client
        self.model = model
        self.base_url = base_url
        self.is_primary = is_primary
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
    2. Secondary: Groq (high-speed open-source reasoning model, e.g. openai/gpt-oss-120b)
    3. Tertiary: MAQ AI Gateway / SLM (OpenAI-compatible internal gateway, e.g. qwen-3.8-27b)
    """

    COOLDOWN_SECONDS = 180  # After 3 minutes, give an unhealthy provider another chance

    def __init__(self, providers: List[ProviderSpec]):
        if not providers:
            raise ValueError(
                "No LLM providers are configured. Please set at least one of:\n"
                "- AZURE_OPENAI_APIKEY & AZURE_OPENAI_ENDPOINT\n"
                "- GROQ_API_KEY (with GROQ_MODEL, e.g. openai/gpt-oss-120b)\n"
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
            if provider.is_healthy:
                healthy_indices.append(idx)
            elif (now - provider.last_failure_time) > self.COOLDOWN_SECONDS:
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

            for idx in candidate_indices:
                provider = self.providers[idx]
                effective_options = dict(options) if options is not None else {}
                effective_options["model"] = provider.model

                logger.info(
                    f"[LLM Dispatch] Routing request to '{provider.name}' (Model: {provider.model})..."
                )

                try:
                    res = provider.client.get_response(
                        messages=messages,
                        stream=stream,
                        options=effective_options,
                        compaction_strategy=compaction_strategy,
                        tokenizer=tokenizer,
                        function_invocation_kwargs=function_invocation_kwargs,
                        client_kwargs=client_kwargs,
                        middleware=middleware,
                    )
                    if asyncio.iscoroutine(res):
                        res = await res

                    provider.mark_success()
                    self.active_provider_index = idx
                    self.model = provider.model
                    return res

                except Exception as exc:
                    err_summary = f"{type(exc).__name__}: {str(exc)}"
                    provider.mark_failed(exc)
                    errors.append(f"[{provider.name}]: {err_summary}")
                    logger.warning(
                        f"[LLM Fallback] Provider '{provider.name}' failed ({err_summary}). "
                        f"Attempting next provider in fallback chain..."
                    )

            # If all candidate providers failed
            all_errors_str = " | ".join(errors)
            logger.error(f"[LLM Fallback] All LLM providers exhausted: {all_errors_str}")
            raise RuntimeError(f"All LLM providers failed: {all_errors_str}")

        if stream:
            # Non-streaming is standard across all agents in DevFoundry, but handle stream if requested
            return _execute_with_fallback()
        return _execute_with_fallback()


def create_fallback_client() -> FallbackChatCompletionClient:
    """Builds the configured FallbackChatCompletionClient according to available environment variables."""
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
            logger.info(f"[LLM Config] Configured Primary Provider: Azure OpenAI ({azure_deployment})")
        except Exception as e:
            logger.warning(f"[LLM Config] Failed to initialize Azure OpenAI client: {e}")

    # 2. Secondary: Groq
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    groq_base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"

    if groq_key:
        try:
            client = OpenAIChatCompletionClient(
                model=groq_model,
                api_key=groq_key,
                base_url=groq_base_url,
            )
            providers.append(
                ProviderSpec(
                    provider_id="groq",
                    name="Groq",
                    client=client,
                    model=groq_model,
                    base_url=groq_base_url,
                )
            )
            logger.info(f"[LLM Config] Configured Secondary Provider: Groq ({groq_model})")
        except Exception as e:
            logger.warning(f"[LLM Config] Failed to initialize Groq client: {e}")

    # 3. Tertiary: MAQ AI Gateway / SLM
    maq_key = os.getenv("MAQ_AI_API_KEY", "").strip()
    maq_base_url = os.getenv("MAQ_AI_BASE_URL", "https://llm.maqsoftware.net/v1").strip()
    maq_model = os.getenv("MAQ_AI_MODEL", "qwen-3.8-27b").strip() or "qwen-3.8-27b"

    if maq_key:
        try:
            client = OpenAIChatCompletionClient(
                model=maq_model,
                api_key=maq_key,
                base_url=maq_base_url,
            )
            providers.append(
                ProviderSpec(
                    provider_id="maq_ai",
                    name="MAQ AI (SLM)",
                    client=client,
                    model=maq_model,
                    base_url=maq_base_url,
                )
            )
            logger.info(f"[LLM Config] Configured Tertiary Provider: MAQ AI ({maq_model})")
        except Exception as e:
            logger.warning(f"[LLM Config] Failed to initialize MAQ AI client: {e}")

    if not providers:
        logger.error(
            "[LLM Config] No valid API keys found for Azure OpenAI, Groq, or MAQ AI. "
            "Please check .env configuration."
        )

    return FallbackChatCompletionClient(providers)
