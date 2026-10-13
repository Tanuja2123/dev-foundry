"""
Guardrails Layer for DevFoundry Multi-Agent System.

Provides central entry points and fail-closed validation boundaries using openai-guardrails
configured exclusively against local SLM endpoints (e.g. MAQ AI Gateway / Qwen SLM).
"""

from guardrails import GuardrailTripwireTriggered

from .client import (
    GuardrailResultSummary,
    build_guardrails_client,
    check_and_call,
    check_input_guardrail,
    check_output_guardrail,
    check_requirements_agent_input,
    check_requirements_agent_output,
)
from .config import (
    is_guardrails_enabled,
    get_guardrails_slm_config,
    ORCHESTRATOR_INPUT_CONFIG_PATH,
    ORCHESTRATOR_OUTPUT_CONFIG_PATH,
    REQUIREMENTS_INPUT_CONFIG_PATH,
    REQUIREMENTS_OUTPUT_CONFIG_PATH,
    ALLOWED_DOMAINS,
    ORCHESTRATOR_ALLOWED_SCOPE,
    REQUIREMENTS_AGENT_ALLOWED_SCOPE,
)

__all__ = [
    "GuardrailTripwireTriggered",
    "GuardrailResultSummary",
    "build_guardrails_client",
    "check_and_call",
    "check_input_guardrail",
    "check_output_guardrail",
    "check_requirements_agent_input",
    "check_requirements_agent_output",
    "is_guardrails_enabled",
    "get_guardrails_slm_config",
    "ORCHESTRATOR_INPUT_CONFIG_PATH",
    "ORCHESTRATOR_OUTPUT_CONFIG_PATH",
    "REQUIREMENTS_INPUT_CONFIG_PATH",
    "REQUIREMENTS_OUTPUT_CONFIG_PATH",
    "ALLOWED_DOMAINS",
    "ORCHESTRATOR_ALLOWED_SCOPE",
    "REQUIREMENTS_AGENT_ALLOWED_SCOPE",
]
