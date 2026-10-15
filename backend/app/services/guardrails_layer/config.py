import os
from pathlib import Path
from typing import Dict, Any, List
from dotenv import load_dotenv

# Ensure environment is loaded from repo root and backend
_current_dir = Path(__file__).resolve().parent
_repo_root = _current_dir.parents[3]
_backend_dir = _current_dir.parents[2]

if (_repo_root / ".env").exists():
    load_dotenv(_repo_root / ".env", override=False)
if (_backend_dir / ".env").exists():
    load_dotenv(_backend_dir / ".env", override=False)
load_dotenv(override=False)

# Directory containing guardrail configuration files
CONFIGS_DIR = _current_dir / "configs"
ORCHESTRATOR_INPUT_CONFIG_PATH = CONFIGS_DIR / "orchestrator_input.json"
ORCHESTRATOR_OUTPUT_CONFIG_PATH = CONFIGS_DIR / "orchestrator_output.json"
REQUIREMENTS_INPUT_CONFIG_PATH = CONFIGS_DIR / "requirements_agent_input.json"
REQUIREMENTS_OUTPUT_CONFIG_PATH = CONFIGS_DIR / "requirements_agent_output.json"


def is_guardrails_enabled() -> bool:
    """Returns True if guardrails enforcement is enabled."""
    val = os.getenv("GUARDRAILS_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes", "on")


def get_guardrails_slm_config() -> Dict[str, str]:
    """
    Reads SLM configuration for Guardrails from environment variables.
    Checks GUARDRAILS_*, LOCAL_SLM_*, and MAQ_AI_* in priority order.
    Never hardcodes credentials or endpoint URLs.
    """
    base_url = (
        os.getenv("GUARDRAILS_BASE_URL")
        or os.getenv("MAQ_AI_BASE_URL")
        or os.getenv("LOCAL_SLM_BASE_URL")
        or "https://indiaai.maqsoftware.net/v1"
    ).strip()

    api_key = (
        os.getenv("MAQ_AI_API_KEY")
        or os.getenv("GUARDRAILS_API_KEY")
        or os.getenv("LOCAL_SLM_API_KEY")
        or os.getenv("LOCAL_SLM_APIKEY")
        or ""
    ).strip()

    model = (
        os.getenv("GUARDRAILS_MODEL")
        or os.getenv("MAQ_AI_MODEL")
        or os.getenv("LOCAL_SLM_MODEL")
        or "qwen-3.8-27b"
    ).strip()

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


# Sensible defaults for DevFoundry domain and scope
ALLOWED_DOMAINS: List[str] = [
    "dev.azure.com",
    "azure.com",
    "github.com",
    "microsoft.com",
    "maqsoftware.net",
    "localhost",
    "127.0.0.1",
]

ORCHESTRATOR_ALLOWED_SCOPE = (
    "DevFoundry AI-powered software development assistant. Allowed scope covers "
    "conversational greetings (such as hello, hi, hey, greetings, pleasantries), introductory dialogue, "
    "software requirements gathering, Product Requirements Documents (PRD), "
    "Technical Requirements Documents (TRD), architecture and tech stack selection, "
    "software architecture planning, repository scaffolding and file structure, "
    "and Azure DevOps project/repo setup. Polite greetings and conversational pleasantries are explicitly on-topic."
)

REQUIREMENTS_AGENT_ALLOWED_SCOPE = (
    "Requirements Agent allowed scope: conversational greetings, pleasantries, "
    "gathering, analyzing, clarifying, drafting, and updating "
    "software functional and non-functional requirements, Product Requirements Documents (PRD), "
    "Technical Requirements Documents (TRD), technology stack selection, system architecture, "
    "API design, user stories, and acceptance criteria. Greetings and conversational pleasantries are on-topic."
)
