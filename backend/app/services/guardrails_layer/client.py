import os
import re
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from openai import AsyncOpenAI
from guardrails import (
    GuardrailsAsyncOpenAI,
    GuardrailTripwireTriggered,
)
from guardrails.runtime import (
    load_pipeline_bundles,
    load_config_bundle,
    instantiate_guardrails,
    run_guardrails,
)
from guardrails.types import GuardrailResult
from guardrails.registry import default_spec_registry

from app.core.logger import logger
from .config import (
    is_guardrails_enabled,
    get_guardrails_slm_config,
    ORCHESTRATOR_INPUT_CONFIG_PATH,
    ORCHESTRATOR_OUTPUT_CONFIG_PATH,
    REQUIREMENTS_INPUT_CONFIG_PATH,
    REQUIREMENTS_OUTPUT_CONFIG_PATH,
)
# Ensure custom checks and aliases are registered
from . import custom_checks  # noqa: F401


@dataclass
class GuardrailResultSummary:
    """Standardized result object returned by check_and_call and boundary checkers."""
    allowed: bool
    response_text: Optional[str] = None
    blocked_reason: Optional[str] = None
    guardrail_name: Optional[str] = None
    info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "response_text": self.response_text,
            "blocked_reason": self.blocked_reason,
            "guardrail_name": self.guardrail_name,
        }


def build_guardrails_client(
    config: Union[str, Path, Dict[str, Any]],
    raise_guardrail_errors: bool = True,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    **kwargs: Any,
) -> GuardrailsAsyncOpenAI:
    """
    Constructs a GuardrailsAsyncOpenAI instance configured against the local SLM.
    Never hardcodes endpoints or credentials; reads from SLM environment variables.
    Fails closed with raise_guardrail_errors=True by default.
    """
    slm_cfg = get_guardrails_slm_config()
    effective_base_url = (base_url or slm_cfg["base_url"]).strip()
    effective_api_key = (api_key or slm_cfg["api_key"] or "dummy-local-slm-key").strip()

    # If config is a Path or str pointing to file, ensure it exists
    if isinstance(config, (str, Path)):
        cfg_path = Path(config)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Guardrails config file not found: {cfg_path}")

    return GuardrailsAsyncOpenAI(
        config=config,
        raise_guardrail_errors=raise_guardrail_errors,
        base_url=effective_base_url,
        api_key=effective_api_key,
        **kwargs,
    )


async def check_and_call(
    client: GuardrailsAsyncOpenAI,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    **kwargs: Any,
) -> GuardrailResultSummary:
    """
    Primary entry point: executes chat completion through GuardrailsAsyncOpenAI.
    Catches GuardrailTripwireTriggered, extracts failure metadata, and fails closed.
    
    Returns:
        GuardrailResultSummary with keys {allowed, response_text, blocked_reason, guardrail_name}.
    """
    slm_cfg = get_guardrails_slm_config()
    effective_model = model or slm_cfg["model"]

    try:
        res = await client.chat.completions.create(
            model=effective_model,
            messages=messages,
            **kwargs,
        )
        content = res.choices[0].message.content or ""
        return GuardrailResultSummary(
            allowed=True,
            response_text=content,
            blocked_reason=None,
            guardrail_name=None,
        )

    except GuardrailTripwireTriggered as exc:
        guardrail_result = getattr(exc, "guardrail_result", None)
        info = guardrail_result.info if guardrail_result else {}
        guardrail_name = info.get("guardrail_name") or "GuardrailTripwire"

        # Determine readable blocked reason
        blocked_reason = (
            info.get("reason")
            or (info.get("blocked_reasons")[0] if info.get("blocked_reasons") else None)
            or f"Request violated guardrail policy: {guardrail_name}"
        )

        logger.warning(
            f"[Guardrails Audit] TRIPWIRE TRIGGERED: guardrail='{guardrail_name}' | "
            f"reason='{blocked_reason}' | info={info}"
        )

        return GuardrailResultSummary(
            allowed=False,
            response_text=None,
            blocked_reason=str(blocked_reason),
            guardrail_name=guardrail_name,
            info=info,
        )

    except Exception as exc:
        logger.error(f"[Guardrails Audit] Exception during check_and_call (fail-closed): {exc}")
        # Fail closed on unexpected errors
        raise


@dataclass
class _SLMContext:
    guardrail_llm: AsyncOpenAI

    def get_conversation_history(self) -> None:
        return None


def _get_slm_context() -> _SLMContext:
    slm_cfg = get_guardrails_slm_config()
    client = AsyncOpenAI(
        base_url=slm_cfg["base_url"],
        api_key=slm_cfg["api_key"] or "dummy-local-slm-key",
    )
    return _SLMContext(guardrail_llm=client)


# Security Priority Hierarchy
# Tier 1 (100): Adversarial attacks (Jailbreak, Prompt Injection, System Prompt Leakage)
# Tier 2 (50):  Harmful / Inappropriate Content (NSFW Text, NSFW, Moderation)
# Tier 3 (10):  Scope & Topic Mismatches (Off Topic Prompts)
GUARDRAIL_PRIORITY: Dict[str, int] = {
    "Jailbreak": 100,
    "Prompt Injection Detection": 100,
    "Prompt Injection": 100,
    "System Prompt Leakage": 100,
    "NSFW Text": 50,
    "NSFW": 50,
    "Moderation": 50,
    "Off Topic Prompts": 10,
}


def get_guardrail_priority(name: str) -> int:
    return GUARDRAIL_PRIORITY.get(name, 0)


def select_prioritized_tripwire(tripwires: List[GuardrailResult]) -> GuardrailResult:
    """
    Selects the most critical guardrail violation according to the security hierarchy:
    Adversarial Attacks (Jailbreak / Prompt Injection) > Harmful Content (NSFW) > Scope (Off-Topic).
    If two guardrails share the same priority tier, the one with higher confidence score is chosen.
    """
    def _rank(item: GuardrailResult) -> Tuple[int, float]:
        name = (item.info or {}).get("guardrail_name", "")
        priority = get_guardrail_priority(name)
        confidence = (item.info or {}).get("confidence", 0.0)
        try:
            conf_val = float(confidence)
        except (ValueError, TypeError):
            conf_val = 0.0
        return (priority, conf_val)

    return max(tripwires, key=_rank)


def _format_tripwire_summary(guardrail_result: GuardrailResult, all_tripwires: Optional[List[GuardrailResult]] = None) -> GuardrailResultSummary:
    info = guardrail_result.info if guardrail_result else {}
    guardrail_name = info.get("guardrail_name") or "Input Guardrail"
    blocked_reason = (
        info.get("reason")
        or (info.get("blocked_reasons")[0] if info.get("blocked_reasons") else None)
        or f"Input violated guardrail '{guardrail_name}'"
    )
    all_names = [r.info.get("guardrail_name") for r in (all_tripwires or [guardrail_result]) if r.info]
    logger.warning(
        f"[Guardrails Audit] INPUT TRIPWIRE TRIGGERED: guardrail='{guardrail_name}' | "
        f"all_triggered={all_names} | reason='{blocked_reason}' | info={info}"
    )
    return GuardrailResultSummary(
        allowed=False,
        response_text=None,
        blocked_reason=str(blocked_reason),
        guardrail_name=guardrail_name,
        info=info,
    )


async def check_input_guardrail(
    text: str,
    config_path: Optional[Union[str, Path]] = None,
) -> GuardrailResultSummary:
    """
    Validates user input against the Orchestrator Input Guardrails:
    - Jailbreak (adversarial attacks, instruction overrides)
    - Prompt Injection Detection
    - NSFW Text (harassment, profanity, violence)
    - Off-Topic Prompts
    Applies security hierarchy when multiple tripwires trigger concurrently.
    """
    if not is_guardrails_enabled():
        return GuardrailResultSummary(allowed=True, response_text=text)

    target_cfg = Path(config_path or ORCHESTRATOR_INPUT_CONFIG_PATH)
    if not target_cfg.exists():
        logger.error(f"[Guardrails] Input config file not found at {target_cfg} (fail-closed)")
        return GuardrailResultSummary(
            allowed=False,
            blocked_reason="Guardrails input configuration file missing",
            guardrail_name="ConfigError",
        )

    try:
        data = json.loads(target_cfg.read_text(encoding="utf-8"))
        input_stage = data.get("input") or data
        bundle = load_config_bundle(input_stage)
        guardrails_instances = instantiate_guardrails(bundle)
        ctx = _get_slm_context()
        results = await run_guardrails(
            ctx=ctx,
            data=text,
            media_type="text/plain",
            guardrails=guardrails_instances,
            raise_guardrail_errors=True,
            suppress_tripwire=True,
        )

        tripwires = [r for r in results if r.tripwire_triggered]
        if tripwires:
            selected = select_prioritized_tripwire(tripwires)
            return _format_tripwire_summary(selected, all_tripwires=tripwires)

        return GuardrailResultSummary(allowed=True, response_text=text)

    except GuardrailTripwireTriggered as exc:
        guardrail_result = getattr(exc, "guardrail_result", None)
        return _format_tripwire_summary(guardrail_result)
    except Exception as exc:
        logger.error(f"[Guardrails Audit] Input validation error (fail-closed): {exc}")
        return GuardrailResultSummary(
            allowed=False,
            blocked_reason=f"Input validation error: {str(exc)}",
            guardrail_name="ExecutionError",
        )


async def check_output_guardrail(
    text: str,
    config_path: Optional[Union[str, Path]] = None,
    redact_pii: bool = True,
) -> GuardrailResultSummary:
    """
    Validates synthesized output against the Orchestrator Output Guardrails:
    - Contains PII (redacted or blocked)
    - URL Filter / Restrict URLs
    """
    if not is_guardrails_enabled():
        return GuardrailResultSummary(allowed=True, response_text=text)

    target_cfg = Path(config_path or ORCHESTRATOR_OUTPUT_CONFIG_PATH)
    if not target_cfg.exists():
        logger.error(f"[Guardrails] Output config file not found at {target_cfg} (fail-closed)")
        return GuardrailResultSummary(
            allowed=False,
            blocked_reason="Guardrails output configuration file missing",
            guardrail_name="ConfigError",
        )

    try:
        data = json.loads(target_cfg.read_text(encoding="utf-8"))
        output_stage = data.get("output") or data

        ctx = _get_slm_context()
        sanitized_text = text

        if redact_pii and "guardrails" in output_stage:
            pii_specs = [g for g in output_stage["guardrails"] if g.get("name") == "Contains PII"]
            other_specs = [g for g in output_stage["guardrails"] if g.get("name") != "Contains PII"]

            if pii_specs:
                pii_copy = [dict(g, config=dict(g.get("config", {}), block=False)) for g in pii_specs]
                pii_bundle = load_config_bundle({"guardrails": pii_copy, "version": 1})
                pii_instances = instantiate_guardrails(pii_bundle)
                pii_results = await run_guardrails(
                    ctx=ctx,
                    data=text,
                    media_type="text/plain",
                    guardrails=pii_instances,
                    raise_guardrail_errors=True,
                    suppress_tripwire=False,
                )
                for r in pii_results:
                    if "checked_text" in r.info:
                        sanitized_text = r.info["checked_text"]

            if other_specs:
                other_bundle = load_config_bundle({"guardrails": other_specs, "version": 1})
                other_instances = instantiate_guardrails(other_bundle)
                url_scan_text = re.sub(r'([.?!])\s*(\n+)', r'\1 \2', sanitized_text)
                await run_guardrails(
                    ctx=ctx,
                    data=url_scan_text,
                    media_type="text/plain",
                    guardrails=other_instances,
                    raise_guardrail_errors=True,
                    suppress_tripwire=False,
                )
        else:
            bundle = load_config_bundle(output_stage)
            guardrails_instances = instantiate_guardrails(bundle)
            url_scan_text = re.sub(r'([.?!])\s*(\n+)', r'\1 \2', text)
            results = await run_guardrails(
                ctx=ctx,
                data=url_scan_text,
                media_type="text/plain",
                guardrails=guardrails_instances,
                raise_guardrail_errors=True,
                suppress_tripwire=False,
            )
            for r in results:
                if r.info.get("guardrail_name") == "Contains PII" and "checked_text" in r.info:
                    sanitized_text = r.info["checked_text"]

        return GuardrailResultSummary(
            allowed=True,
            response_text=sanitized_text,
        )


    except GuardrailTripwireTriggered as exc:
        guardrail_result = getattr(exc, "guardrail_result", None)
        info = guardrail_result.info if guardrail_result else {}
        guardrail_name = info.get("guardrail_name") or "Output Guardrail"
        blocked_reason = (
            info.get("reason")
            or (info.get("blocked_reasons")[0] if info.get("blocked_reasons") else None)
            or f"Output violated guardrail '{guardrail_name}'"
        )
        logger.warning(
            f"[Guardrails Audit] OUTPUT TRIPWIRE TRIGGERED: guardrail='{guardrail_name}' | "
            f"reason='{blocked_reason}' | info={info}"
        )
        return GuardrailResultSummary(
            allowed=False,
            response_text=None,
            blocked_reason=str(blocked_reason),
            guardrail_name=guardrail_name,
            info=info,
        )
    except Exception as exc:
        logger.error(f"[Guardrails Audit] Output validation error (fail-closed): {exc}")
        return GuardrailResultSummary(
            allowed=False,
            blocked_reason=f"Output validation error: {str(exc)}",
            guardrail_name="ExecutionError",
        )


async def check_requirements_agent_input(text: str) -> GuardrailResultSummary:
    """Validates task input for the Requirements Agent."""
    return await check_input_guardrail(text, config_path=REQUIREMENTS_INPUT_CONFIG_PATH)


async def check_requirements_agent_output(text: str) -> GuardrailResultSummary:
    """Validates generated output for the Requirements Agent."""
    return await check_output_guardrail(text, config_path=REQUIREMENTS_OUTPUT_CONFIG_PATH, redact_pii=False)
