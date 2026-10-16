import asyncio
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional
from fastapi import UploadFile
from dotenv import load_dotenv

from agent_framework import Agent, Message
from agent_framework.openai import OpenAIChatCompletionClient
from supabase import create_client, Client

from app.schemas.conversation import ChatResponse, ConversationItem
from app.services.agents.requirements_agent import process_uploaded_document, get_requirements_agent
from app.services.agents.repo_setup_agent import get_repo_setup_agent
from app.services.mcp.ado_mcp_server import sanitize_ado_project_name
from app.services import project_store as store
from app.services import progress, tech_fit
from app.services.backlog_service import run_backlog_operation
from app.services.repo_structure import layer_changes, render_structure_tree
from app.services.scaffold_service import describe_toolchains
from app.services.workflow_routing import (RoutingDecision, blocked_reply, effective_stage, next_steps,
                                           parse_routing_decision, routing_context, validate_decision,
                                           apply_intent_rules, answers_different_product_question, allowed_operations)
from app.utils.document_markdown import prd_json_to_markdown, trd_json_to_markdown
from app.core.logger import logger

from app.services.llm_client import (AllProvidersFailedError, FallbackChatCompletionClient, create_fallback_client,
                                    estimate_request_tokens, orchestrator_provider_order)
from app.services.guardrails_layer import (
    check_input_guardrail,
    check_output_guardrail,
    check_requirements_agent_input,
    check_requirements_agent_output,
    is_guardrails_enabled,
)

# Load variables from .env file (checks repo root, backend folder, and default)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env"))
load_dotenv() # also try default location

_warned_key_role = False
_supabase_client: Optional[Client] = None

def supabase_key_role(key: str) -> str:
    """'service_role', 'anon' or 'unknown' (supports legacy JWT keys and the newer sb_secret_/sb_publishable_ keys)."""
    if key.startswith("sb_secret_"):
        return "service_role"
    if key.startswith("sb_publishable_"):
        return "anon"
    try:
        import base64
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role") or "unknown"
    except Exception:
        return "unknown"

def get_supabase_client() -> Client:
    """Backend-only Supabase client. Use the service-role key: the tables are closed to the anon key (migration 003)."""
    global _supabase_client, _warned_key_role
    if _supabase_client is not None:
        return _supabase_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        logger.error("[Supabase] Credentials missing in environment (.env).")
        raise ValueError("Supabase credentials not found in environment.")
    if not _warned_key_role and supabase_key_role(key) != "service_role":
        _warned_key_role = True
        logger.warning("[Supabase] The configured key is not a service-role key. After migration 003 the tables are "
                       "closed to the anon key; set SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) to the service-role key.")

    _supabase_client = create_client(url, key)
    logger.info("[Supabase] Initialized shared singleton client with connection pooling.")
    return _supabase_client

def _resolve_project_stage(project_row: Optional[Dict[str, Any]]) -> str:
    """Derives a normalized stage key from Project table columns."""
    if not project_row:
        return "requirements"

    stage = project_row.get("workflow_stage") or ""
    repo_url = project_row.get("repo_url")
    ado_project_id = project_row.get("ado_project_id")
    has_trd = bool(project_row.get("trd"))
    has_prd = bool(project_row.get("prd"))

    if stage == "COMPLETE":
        return "completed"
    if repo_url or stage in ["STRUCTURE_REVIEW", "REPOSITORY_REVIEW"]:
        return "repo_created"
    if ado_project_id or stage == "PROJECT_REVIEW":
        return "project_created"
    if has_trd or stage == "TRD_REVIEW":
        return "trd_created"
    if has_prd or stage == "PRD_REVIEW":
        return "prd_created"
    return "requirements"

_in_memory_conversations: Dict[str, Dict[str, Any]] = {}

def get_conversations(user_id: str) -> List[ConversationItem]:
    """Conversations owned by the signed-in user, newest first."""
    supabase = get_supabase_client()
    try:
        response = (supabase.table("Conversation").select("*").eq("user_id", user_id)
                    .order("created_at", desc=True).execute())
        conv_rows = response.data or []

        # Batch lookup stage information from Project table
        projects_by_conv: Dict[str, dict] = {}
        conv_ids = [r["conv_id"] for r in conv_rows if r.get("conv_id")]
        if conv_ids:
            try:
                proj_res = (
                    supabase.table("Project")
                    .select("conv_id, workflow_stage, prd, trd, ado_project_id, repo_url")
                    .in_("conv_id", conv_ids)
                    .execute()
                )
                for p_row in (proj_res.data or []):
                    c_id = p_row.get("conv_id")
                    if c_id:
                        projects_by_conv[c_id] = p_row
            except Exception as pe:
                logger.warning(f"[Supabase] Could not batch-fetch project stages: {pe}")

        items = []
        for row in conv_rows:
            conv_id = row.get("conv_id")
            title = row.get("title", "New Project")
            if conv_id:
                stage = _resolve_project_stage(projects_by_conv.get(conv_id))
                items.append(ConversationItem(conversation_id=conv_id, title=title, stage=stage))
        logger.info(f"[Supabase] Retrieved {len(items)} conversations for user '{user_id}' from DB.")
        return items
    except Exception as e:
        logger.warning(f"[Supabase] Error fetching conversations: {str(e)}; falling back to local memory store.")
        items = []
        for c_id, row in _in_memory_conversations.items():
            if row.get("user_id") == user_id:
                items.append(ConversationItem(
                    conversation_id=c_id,
                    title=row.get("title", "New Project"),
                    stage="requirements",
                ))
        return items

def create_new_conversation(user_id: str, title: str = "New Project") -> ConversationItem:
    supabase = get_supabase_client()
    new_id = str(uuid.uuid4())
    data = {
        "conv_id": new_id,
        "user_id": user_id,
        "content": [],
        "title": title
    }
    try:
        supabase.table("Conversation").insert(data).execute()
        logger.info(f"[Supabase] Created new conversation '{new_id}' (Title: '{title}')")
        _in_memory_conversations[new_id] = data
        return ConversationItem(conversation_id=new_id, title=title, stage="requirements")
    except Exception as e:
        logger.error(f"[Supabase] Failed to create new conversation: {str(e)}")
        raise

def delete_conversation(conversation_id: str, user_id: str) -> bool:
    """Deletes the conversation if the user owns it (its Project/Repository rows cascade). False if not found/owned."""
    if not get_conversation_by_id(conversation_id, user_id):
        return False

    _in_memory_conversations.pop(conversation_id, None)
    supabase = get_supabase_client()
    try:
        supabase.table("Conversation").delete().eq("conv_id", conversation_id).eq("user_id", user_id).execute()
        logger.info(f"[Supabase] Deleted conversation '{conversation_id}'")
        return True
    except Exception as e:
        logger.error(f"[Supabase] Failed to delete conversation '{conversation_id}': {str(e)}")
        raise


def get_conversation_by_id(conversation_id: str, user_id: str) -> Optional[Dict]:
    """The conversation if it exists AND belongs to the user; None otherwise (callers answer 404 either way)."""
    try:
        uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return None

    supabase = get_supabase_client()
    try:
        response = (supabase.table("Conversation").select("*").eq("conv_id", conversation_id)
                    .eq("user_id", user_id).execute())
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        logger.error(f"[Supabase] Error retrieving conversation '{conversation_id}': {str(e)}")
        raise


# LLM and Agent Framework Client configuration (Azure OpenAI with Local SLM Fallback)

# Agent Framework Client configuration (Tiered Fallback: Azure OpenAI -> Groq -> MAQ AI SLM)

def get_agent_framework_client(fallback: bool = False) -> OpenAIChatCompletionClient:
    """Returns Agent Framework client, prioritizing the tiered fallback client."""
    if fallback:
        return get_local_slm_client()
    try:
        return create_fallback_client()
    except Exception as e:
        logger.warning(f"[LLM Client] Failed to create tiered fallback client ({e}). Using direct client.")
        try:
            return get_azure_openai_client()
        except Exception:
            return get_local_slm_client()

def get_azure_openai_client() -> OpenAIChatCompletionClient:
    """Configures primary Azure OpenAI client for Microsoft Agent Framework."""
    api_key = os.getenv("AZURE_OPENAI_APIKEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_VERSION", "2024-12-01-preview")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
    
    if not api_key or not endpoint:
        logger.error("[Azure OpenAI] Credentials missing in environment.")
        raise ValueError("Azure OpenAI credentials not found in environment.")
        
    return OpenAIChatCompletionClient(
        model=deployment,
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
    )

def get_local_slm_config() -> Dict[str, str]:
    """Retrieves Local SLM fallback configuration from environment, prioritizing MAQ_AI_API_KEY."""
    api_key = (
        os.getenv("MAQ_AI_API_KEY")
        or os.getenv("LOCAL_SLM_API_KEY")
        or os.getenv("LOCAL_SLM_APIKEY")
        or ""
    ).strip()
    base_url = (
        os.getenv("MAQ_AI_BASE_URL")
        or os.getenv("LOCAL_SLM_BASE_URL")
        or "https://indiaai.maqsoftware.net/v1"
    ).strip()
    model = (
        os.getenv("MAQ_AI_MODEL")
        or os.getenv("LOCAL_SLM_MODEL")
        or "qwen-3.8-27b"
    ).strip()
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }

def get_local_slm_client() -> OpenAIChatCompletionClient:
    """Configures fallback Local SLM client for Microsoft Agent Framework."""
    config = get_local_slm_config()
    if not config["api_key"]:
        logger.error("[Local SLM] MAQ_AI_API_KEY / LOCAL_SLM_API_KEY missing in environment.")
        raise ValueError("Local SLM credentials not found in environment (MAQ_AI_API_KEY missing).")
        
    return OpenAIChatCompletionClient(
        model=config["model"],
        api_key=config["api_key"],
        base_url=config["base_url"],
    )

# Workflow state persistence (Project / Repository tables)

STRUCTURE_PLACEHOLDER = "[[REPOSITORY_STRUCTURE]]"
COMMANDS_PLACEHOLDER = "[[REPOSITORY_COMMANDS]]"

# Shown to the user instead of technical errors (the details are logged with the same reference)
SERVICE_UNAVAILABLE_MESSAGE = ("⚠️ The AI service is busy or unavailable right now, so I couldn't process your message. "
                               "Nothing was changed - please try again in a minute. (Reference: {reference})")
UNEXPECTED_ERROR_MESSAGE = ("⚠️ Something went wrong while processing your message. Nothing was changed - please try "
                            "again. (Reference: {reference})")
STRUCTURE_STAGES = ("REPOSITORY_CREATED", "STRUCTURE_REVIEW", "STRUCTURE_PUSHED")
SCAFFOLD_OPERATIONS = ("CREATE_REPOSITORY", "GENERATE_REPOSITORY_STRUCTURE", "UPDATE_REPOSITORY_STRUCTURE")

def parse_agent_json(text: str) -> Optional[Dict]:
    """Extracts the JSON object a child agent returned (tolerates ```json fences)."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None

def agent_json_error(text: str) -> Optional[str]:
    """Why a child agent's output is not a usable JSON object (None when it is)."""
    if not text or not text.strip():
        return "empty output"
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return "no JSON object found"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return f"{e.msg} at character {e.pos}"
    if not isinstance(data, dict) or not data.get("status"):
        return "JSON object without a 'status' field"
    return None


DEFAULT_TECH_CHANGE_CONFIRM_THRESHOLD = 3.0


def tech_change_confirm_threshold() -> float:
    """How much better (on the agent's 1-10 fit scale) the current technology must fit before a change needs confirmation."""
    try:
        return float(os.getenv("TECH_CHANGE_CONFIRM_THRESHOLD") or DEFAULT_TECH_CHANGE_CONFIRM_THRESHOLD)
    except ValueError:
        return DEFAULT_TECH_CHANGE_CONFIRM_THRESHOLD


def tech_change_needing_confirmation(raw_output: str) -> Optional[Dict]:
    """
    The Requirements_Agent's assessment of a requested technology change, when the requested choice fits this project
    clearly worse than the current one (gap >= threshold) and the user has not confirmed it yet. None otherwise.
    """
    data = parse_agent_json(raw_output) or {}
    assessment = data.get("tech_change_assessment")
    if data.get("status") != "COMPLETE" or not isinstance(assessment, dict) or assessment.get("user_confirmed") is True:
        return None
    try:
        gap = float(assessment.get("current_fit")) - float(assessment.get("requested_fit"))
    except (TypeError, ValueError):
        return None
    return assessment if gap >= tech_change_confirm_threshold() else None


_PROCEED_ON_ASSUMPTIONS = re.compile(
    r"(make|use|with|go with|fill in)\s+(any\s+|some\s+|reasonable\s+|sensible\s+)*assumptions|"
    r"assume (the rest|anything|whatever)|just (generate|write|create|draft) (it|the prd)|"
    r"proceed (anyway|without|with assumptions)|no more questions", re.IGNORECASE)


def asked_to_proceed(user_query: str, history: Optional[List[Dict]] = None) -> bool:
    """True when the user explicitly asked to write the PRD on assumptions (in this message or one of their last
    three messages, e.g. a description ending "Make reasonable assumptions for anything else.")."""
    recent_user = [m.get("text") or "" for m in (history or []) if m.get("role") == "user"][-3:]
    return any(_PROCEED_ON_ASSUMPTIONS.search(text or "") for text in [user_query, *recent_user])


def missing_readiness_items(raw_output: str) -> List[str]:
    """R1-R5 items the Requirements_Agent itself marked "missing" in a PRD it nevertheless returned as COMPLETE."""
    data = parse_agent_json(raw_output) or {}
    readiness = data.get("readiness")
    if data.get("status") != "COMPLETE" or not isinstance(readiness, dict):
        return []
    return sorted(key for key, value in readiness.items() if str(value).strip().lower() == "missing")

def document_not_saved_message(operation: Optional[str]) -> str:
    document = "TRD" if "TRD" in (operation or "") else "PRD"
    return (f"I couldn't save the updated {document} this time, so nothing has changed and the stored {document} is "
            f"still the current one.\n\n---\n\nPlease ask again in a moment.")


def persist_requirements_output(conversation_id: Optional[str], raw_output: str) -> bool:
    """Stores a completed PRD/TRD in the Project row of the conversation. True when a document was saved."""
    data = parse_agent_json(raw_output)
    if not conversation_id or not data or data.get("status") != "COMPLETE":
        return False
    prd = data.get("structured_project_response")
    trd = data.get("technical_requirements_document")
    if not prd and not trd:
        return False
    try:
        project = store.ensure_project(conversation_id)
        stage = project.get("workflow_stage") or store.STAGE_REQUIREMENTS
        versioned = "prd_version" in project  # migration 002 applied
        fields: Dict = {}
        prd_version = project.get("prd_version") or 0
        if prd:
            fields["prd"] = prd
            if versioned:
                prd_version += 1
                fields["prd_version"] = prd_version
            # Documents can change at any stage; only the requirements stages move forward here.
            if stage in (store.STAGE_REQUIREMENTS, store.STAGE_PRD_REVIEW):
                fields["workflow_stage"] = store.STAGE_PRD_REVIEW
        if trd:
            def _strip_mandated_tags(val: Any) -> Any:
                if isinstance(val, str):
                    return re.sub(r"\s*\(\s*mandated(?:\s+by\s+user)?\s*\)", "", val, flags=re.IGNORECASE).strip()
                elif isinstance(val, list):
                    return [_strip_mandated_tags(x) for x in val]
                elif isinstance(val, dict):
                    return {k: _strip_mandated_tags(v) for k, v in val.items()}
                return val
            trd = _strip_mandated_tags(trd)
            fields["trd"] = trd
            if versioned:
                fields["trd_version"] = (project.get("trd_version") or 0) + 1
                fields["trd_prd_version"] = prd_version
            if stage in (store.STAGE_REQUIREMENTS, store.STAGE_PRD_REVIEW, store.STAGE_TRD_REVIEW):
                fields["workflow_stage"] = store.STAGE_TRD_REVIEW
        store.update_project(project["project_id"], **fields)
        logger.info(f"[ProjectStore] Saved {', '.join(fields).upper()} for conversation '{conversation_id}'")
        return True
    except store.StoreError as e:
        logger.error(f"[ProjectStore] Could not save requirements: {e}")
        return False

def persist_repo_setup_output(conversation_id: Optional[str], raw_output: str) -> None:
    """Stores project/repository proposals from the Repo_Setup_Agent. Created resources are stored by its tools."""
    data = parse_agent_json(raw_output)
    if not conversation_id or not data or data.get("status") != "COMPLETE":
        return
    stage = data.get("workflow_stage")
    try:
        proposal = data.get("project_proposal") or {}
        if stage == "PROJECT_REVIEW" and store.is_placeholder_name(proposal.get("project_name")):
            if proposal.get("project_name"):
                logger.warning(f"[ProjectStore] Ignored placeholder project name {proposal.get('project_name')!r}")
            proposal = {}
        if stage == "PROJECT_REVIEW" and proposal.get("project_name"):
            project = store.ensure_project(conversation_id)
            if not project.get("ado_project_id"):
                versions = {"proposal_trd_version": project.get("trd_version") or 0} if "trd_version" in project else {}
                cleaned_name = sanitize_ado_project_name(proposal["project_name"])
                store.update_project(project["project_id"], project_name=cleaned_name,
                                     project_description=(proposal.get("project_description") or "").strip(),
                                     workflow_stage=store.STAGE_PROJECT_REVIEW, **versions)
        repo_proposal = data.get("repository_proposal") or {}
        if stage in ("PROJECT_CREATED", "REPOSITORY_REVIEW") and not store.is_placeholder_name(repo_proposal.get("repo_name")):
            project = store.get_project(conversation_id)
            if project and project.get("ado_project_id"):
                store.propose_repository_name(project["project_id"], repo_proposal["repo_name"].strip())
                store.update_project(project["project_id"], workflow_stage=store.STAGE_REPOSITORY_REVIEW)
    except store.StoreError as e:
        logger.error(f"[ProjectStore] Could not save repository setup proposal: {e}")

def follow_up_note(conversation_id: Optional[str]) -> str:
    """Outdated artifacts after this turn, for the synthesizer to mention (empty when nothing is outdated)."""
    if not conversation_id:
        return ""
    state = store.get_workflow_state(conversation_id)
    items = store.outdated_items(state) if not state.get("error") else []
    if not items:
        return ""
    lines = "\n".join(f"- {store.OUTDATED_DESCRIPTIONS[item]}" for item in items)
    return ("\n\nFOLLOW-UPS: Because documents changed, these items are now outdated:\n" + lines +
            "\nEnd your answer with a short '**Suggested next steps**' list offering to update them (in that order), "
            "instead of moving the workflow forward.")

def _format_commands(commands: List[Dict]) -> str:
    """The generator steps as run: commands and online initializer requests, with their folder and the version used."""
    lines = []
    for item in commands or []:
        cwd = (item.get("cwd") or ".").strip()
        notes = [f"in {cwd}/"] if cwd not in ("", ".") else []
        if item.get("version"):
            notes.append(str(item["version"]))
        if item.get("initializer"):
            notes.insert(0, f"{item['initializer']} online initializer")
        lines.append(item.get("command", "") + (f"    # {', '.join(notes)}" if notes else ""))
    return "```bash\n" + "\n".join(lines) + "\n```"


def render_structure_for_display(state: Dict) -> Optional[str]:
    """
    Renders the stored structure (the exact files that are / will be pushed) as Markdown: the generator commands that
    produced it, the tree (files added or edited on request are marked), and how it will be committed.
    """
    repository = (state or {}).get("repository") or {}
    files = repository.get("structure") or []
    if not files:
        return None
    pushed = repository.get("status") == store.REPO_PUSHED and repository.get("pushed_structure") in (None, files)
    title = "Pushed repository structure" if pushed else "Proposed repository structure"
    parts = [f"**{title}** (version {repository.get('structure_version')}, {len(files)} files)"]

    scaffold = repository.get("scaffold_structure")
    commands = repository.get("scaffold_commands") or []
    notes: Dict[str, str] = {}
    changes = {"add": [], "edit": [], "delete": []}
    if scaffold:
        changes = layer_changes(scaffold, files)
        notes.update({path: "added" for path in changes["add"]})
        notes.update({path: "edited" for path in changes["edit"]})
    # Directory tree first
    parts.append(render_structure_tree(files, root_name=repository.get("repo_name"), notes=notes))
    if changes["delete"]:
        parts.append("Removed from the generated files: " + ", ".join(f"`{p}`" for p in changes["delete"]))

    # Generator commands directly UNDER the directory tree
    if commands and repository.get("pushed_structure") is not None:
        parts.append("Created by running these commands (their exact output is the first commit"
                     + (", followed by the changes you asked for" if notes or changes["delete"] else "") + "):")
        parts.append(_format_commands(commands))

    if not pushed and repository.get("pushed_structure") is None and scaffold and any(changes.values()):
        counts = ", ".join(f"{len(changes[k])} {label}" for k, label in
                           (("add", "added"), ("edit", "edited"), ("delete", "removed")) if changes[k])
        parts.append(f"On approval this is pushed to `main` in two commits: the generator output ({len(scaffold)} files), "
                     f"then your requested changes ({counts}).")
    return "\n\n".join(parts)


def render_tree_only(state: Dict) -> Optional[str]:
    repository = (state or {}).get("repository") or {}
    files = repository.get("structure") or []
    if not files:
        return None
    scaffold = repository.get("scaffold_structure")
    notes: Dict[str, str] = {}
    if scaffold:
        changes = layer_changes(scaffold, files)
        notes.update({path: "added" for path in changes["add"]})
        notes.update({path: "edited" for path in changes["edit"]})
    return render_structure_tree(files, root_name=repository.get("repo_name"), notes=notes)


def render_commands_for_display(state: Dict) -> Optional[str]:
    repository = (state or {}).get("repository") or {}
    files = repository.get("structure") or []
    commands = repository.get("scaffold_commands") or []
    if not commands or repository.get("pushed_structure") is None:
        return None
    scaffold = repository.get("scaffold_structure")
    notes: Dict[str, str] = {}
    changes = {"add": [], "edit": [], "delete": []}
    if scaffold:
        changes = layer_changes(scaffold, files)
        notes.update({path: "added" for path in changes["add"]})
        notes.update({path: "edited" for path in changes["edit"]})
    title = f"**Pushed repository structure** (version {repository.get('structure_version')}, {len(files)} files)"
    header = ("Created by running these commands (their exact output is the first commit"
              + (", followed by the changes you asked for" if notes or changes["delete"] else "") + "):")
    return f"{title}\n\n{header}\n\n{_format_commands(commands)}"

def partial_success_reply(before: Optional[Dict], after: Optional[Dict], reference: str) -> Optional[str]:
    """
    What a turn really changed (compared with the state before it), rendered by code, for when the final reply step
    failed after tools had already acted. None when nothing changed (then "Nothing was changed" is true).
    """
    if not after or after.get("error"):
        return None
    b_project, a_project = (before or {}).get("project") or {}, after.get("project") or {}
    b_repo, a_repo = (before or {}).get("repository") or {}, after.get("repository") or {}
    done: List[str] = []
    if a_project.get("ado_project_id") and not b_project.get("ado_project_id"):
        done.append(f"The Azure DevOps project **{a_project.get('project_name')}** was created "
                    f"([open it]({a_project.get('ado_project_url')})).")
    if (a_project.get("prd_version") or 0) > (b_project.get("prd_version") or 0):
        done.append(f"The PRD was saved (version {a_project.get('prd_version')}). Ask me to show the PRD to see it.")
    if (a_project.get("trd_version") or 0) > (b_project.get("trd_version") or 0):
        done.append(f"The TRD was saved (version {a_project.get('trd_version')}). Ask me to show the TRD to see it.")
    if a_repo.get("ado_repo_id") and a_repo.get("ado_repo_id") != b_repo.get("ado_repo_id"):
        done.append(f"The repository **{a_repo.get('repo_name')}** was created.")
    pushed_now = a_repo.get("commit_id") and a_repo.get("commit_id") != b_repo.get("commit_id")
    structure_changed = (a_repo.get("structure_version") or 0) > (b_repo.get("structure_version") or 0) or (
        a_repo.get("structure") and not b_repo.get("structure"))
    if pushed_now:
        done.append(f"The structure was pushed to `{a_repo.get('branch') or 'main'}` (commit "
                    f"`{str(a_repo.get('commit_id'))[:7]}`).")
    if len(a_project.get("work_items") or {}) > len(b_project.get("work_items") or {}):
        done.append(f"{len(a_project.get('work_items'))} work items now exist in Azure Boards.")
    if not done and not structure_changed:
        return None
    parts = ["Part of your request was completed, but the AI service is busy, so I couldn't write the full reply "
             f"(Reference: {reference}). Here is what was saved:"]
    parts.append("\n".join(f"- {item}" for item in done) if done else "")
    if structure_changed and not pushed_now:
        tree = render_structure_for_display(after)
        if tree:
            parts.append(tree)
        parts.append("---")
        parts.append("Nothing has been pushed yet: review the structure, then say **push to main** or tell me what "
                     "to change.")
    else:
        parts.append("---")
        parts.append("Tell me how you'd like to continue.")
    return "\n\n".join(p for p in parts if p)


_PUSH_REQUEST = re.compile(r"\b(push\w*|commit\w*|publish\w*)\b", re.IGNORECASE)


def structure_was_shown(history: List[Dict], repository: Dict) -> bool:
    """True when an earlier reply showed the current structure version (the tree is rendered with its version)."""
    marker = f"(version {repository.get('structure_version')},"
    return any(m.get("role") == "assistant" and marker in (m.get("text") or "") for m in history or [])


def push_review_reply(user_query: str, history: List[Dict], state: Optional[Dict]) -> Optional[str]:
    """
    A push needs (1) a message that actually asks to push and (2) the current structure shown to the user before
    (seen live: "approved, create the repo" pushed a structure the user never saw). Returns the structure to review
    instead of pushing, or None when the push may go ahead.
    """
    repository = (state or {}).get("repository") or {}
    if not repository.get("structure"):
        return None                               # nothing to show; the agent explains
    asked = bool(_PUSH_REQUEST.search(user_query or ""))
    if asked and structure_was_shown(history, repository):
        return None
    tree = render_structure_for_display(state) or ""
    intro = ("Before pushing, here is exactly what would be pushed to `main`:" if asked else
             "Nothing has been pushed yet. Here is the repository structure for your review:")
    return f"{intro}\n\n{tree}\n\n---\n\nReview it, then say **push to main** (or tell me what to change)."


def stored_document_reply(document: Optional[str], workflow_state: Optional[Dict]) -> Optional[str]:
    """
    The stored PRD / TRD as a reply, when the user asks to see it. Rendered from the database by code, so the model can
    never show a document that differs from the saved one. None when there is no such document (the model's reply stays).
    """
    project = (workflow_state or {}).get("project") or {}
    if document == "PRD" and project.get("prd"):
        body, name = prd_json_to_markdown(project["prd"]), "Product Requirements Document (PRD)"
        step = ("Tell me what to change in the PRD, or continue with the next step." if project.get("trd") else
                "Tell me what to change, or ask me to create the TRD when you're ready.")
        version = project.get("prd_version")
    elif document == "TRD" and project.get("trd"):
        body, name = trd_json_to_markdown(project["trd"]), "Technical Requirements Document (TRD)"
        step = ("Tell me what to change in the TRD, or continue with the next step." if project.get("project_name") else
                "Tell me what to change, or ask me to proceed with project setup when you're ready.")
        version = project.get("trd_version")
    elif document == "STRUCTURE" and (workflow_state or {}).get("repository", {}).get("structure"):
        tree = render_structure_for_display(workflow_state)
        repo_name = (workflow_state or {}).get("repository", {}).get("repo_name") or "repository"
        pushed = bool((workflow_state or {}).get("repository", {}).get("commit_id"))
        status_text = "pushed to `main`" if pushed else "ready for review before pushing to `main`"
        step = "The repository setup is complete." if pushed else "Review it, then say **push to main** or tell me what to change."
        return f"Here is the proposed repository folder structure for **{repo_name}** ({status_text}):\n\n{tree}\n\n---\n\n{step}"
    else:
        return None
    label = f" (version {version})" if version else ""
    return f"Here is the current, suggested **{name}**{label}:\n\n{body.strip()}\n\n---\n\n{step}"


GUARDRAIL_ERROR_NAMES = {"ServiceUnavailable", "ExecutionError", "ConfigError"}
GUARDRAIL_UNAVAILABLE_MESSAGE = ("The safety check could not run just now, so your message was not processed and nothing "
                                 "changed. Please try again in a minute.")
_GUARDRAIL_REASONS = {
    "Off Topic Prompts": "it doesn't seem related to planning or setting up a software project",
    "NSFW Text": "it contains inappropriate content",
}


def guardrail_block_message(check: Any) -> str:
    """User-facing text for a blocked message: short and honest; the details stay in the log."""
    name = getattr(check, "guardrail_name", None) or ""
    if name in GUARDRAIL_ERROR_NAMES:          # the guardrail model failed: not the user's fault
        return GUARDRAIL_UNAVAILABLE_MESSAGE
    reason = _GUARDRAIL_REASONS.get(name, "it looks like an attempt to change or reveal my instructions")
    return (f"I can't help with that message because {reason}. I can help you plan software: describe a product, "
            "ask about your PRD or TRD, or continue with the next step.")


def presents_document(reply: str) -> bool:
    """Whether a synthesized reply actually shows a PRD/TRD (headings and some length), not a meta answer."""
    text = reply or ""
    return len(text) >= 400 and ("Requirements Document" in text or "(PRD)" in text or "(TRD)" in text) \
        and text.count("\n") >= 5


def _encode_markdown_links(text: str) -> str:
    """Ensure any markdown link [label](url) with spaces in url is percent-encoded so it's clickable."""
    def _replace(match):
        label = match.group(1)
        url = match.group(2)
        clean_url = url.replace(" ", "%20")
        return f"[{label}]({clean_url})"
    return re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', _replace, text)


def _strip_meta_commentary(text: str) -> str:
    """Strips LLM meta-commentary, case reflection, checklists, and rule self-checks."""
    if not text:
        return text
    patterns = [
        r"(?is)\n*-{0,3}\s*That'?s the (?:full,?\s*)?formatted response for\b.*",
        r"(?is)\n*-{0,3}\s*Here'?s a (?:quick\s*)?checklist of what was included\b.*",
        r"(?is)\n*-{0,3}\s*Let me double-check against the rules:?\b.*",
        r"(?is)\n*-{0,3}\s*Everything looks correct\.\s+The response is clean\b.*",
        r"(?is)\n*-{0,3}\s*Ready for the user'?s reply\b.*",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text)
    return text.strip()


def normalize_reply(text: str) -> str:
    """Normalize reply line breaks, strip meta-commentary, strip mandated tags, and encode markdown links."""
    if not text:
        return ""
    if "\\n" in text and text.count("\\n") > text.count("\n"):
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = _strip_meta_commentary(text)
    text = re.sub(r"\s*\(\s*mandated(?:\s+by\s+user)?\s*\)", "", text, flags=re.IGNORECASE)
    text = _encode_markdown_links(text)
    return text


_BLOCK_START = ("#", "-", "*", "+", "|", "`", ">")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“(*_])")
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "Dr.", "Mr.", "Ms.", "No.")
# Sentences that invite the user to act: a closing line, not content
_CLOSING_STARTS = ("please review", "please tell", "please let", "tell me", "let me know", "feel free", "shall i",
                   "would you", "do you want", "you can now", "when you're ready", "approve ")


def _split_sentences(paragraph: str) -> List[str]:
    """Sentences of one plain paragraph (does not split after e.g. / i.e. / etc.)."""
    parts: List[str] = []
    for piece in _SENTENCE_END.split(paragraph.strip()):
        if parts and parts[-1].endswith(_ABBREVIATIONS):
            parts[-1] += " " + piece
        else:
            parts.append(piece)
    return [p for p in parts if p]


def enforce_closing_line(text: str) -> str:
    """
    Every reply ends with a divider and one plain closing sentence. Fixes the common model slips: a trailing divider after
    the closing sentence, a missing divider, or repeated dividers. Replies ending in a list, table, heading or code are
    left as they are.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    while blocks and blocks[-1] == "---":
        blocks.pop()
    if not blocks:
        return ""
    closing = blocks[-1]
    is_plain = "\n" not in closing and not closing.startswith(_BLOCK_START) and not re.match(r"^\d+\.\s", closing)
    if not is_plain:
        return "\n\n".join(blocks)
    # The closing line is ONE sentence: a final paragraph with several sentences keeps all but the last one in the body
    # (seen live: a one-paragraph answer ending "If you have a project in mind, just let me know!" had no divider).
    sentences = _split_sentences(closing)
    if len(sentences) > 1:
        blocks[-1:] = [" ".join(sentences[:-1]), sentences[-1]]
        closing = blocks[-1]
    if len(blocks) < 2:
        return "\n\n".join(blocks)
    body = blocks[:-1]
    had_divider = bool(body) and body[-1] == "---"
    while body and body[-1] == "---":
        body.pop()
    previous = body[-1] if body else ""
    previous_is_closing = (bool(previous) and "\n" not in previous and not previous.startswith(_BLOCK_START)
                           and len(previous) < 300 and len(_split_sentences(previous)) == 1
                           and (previous.rstrip().endswith("?") or previous.lower().startswith(_CLOSING_STARTS)))
    if previous_is_closing and (had_divider or closing.lower().startswith(_CLOSING_STARTS)):
        # two closing lines around the divider (a question or invitation, then another sentence): keep only the first
        # (seen live: "Please review the name and description ... project." --- "Let me know any adjustments ...")
        closing = body.pop()
    return "\n\n".join(body + ["---", closing])


def should_attach_structure_tree(text: str, state: Optional[Dict[str, Any]]) -> bool:
    """
    True when a conversational reply discusses repository creation or the folder structure in STRUCTURE_REVIEW,
    or asks to push/review/update it, but does not yet include the rendered structure tree.
    """
    if not state:
        return False
    repo = state.get("repository") or {}
    files = repo.get("structure") or []
    if not files or repo.get("pushed_structure") is not None or repo.get("commit_id"):
        return False
    marker = f"(version {repo.get('structure_version')},"
    if marker in text or STRUCTURE_PLACEHOLDER in text:
        return False
    lower = text.lower()
    keywords = (
        "current structure", "repository structure", "folder structure", "proposed structure",
        "generate or push", "push the current", "push it", "push to main", "push to the main branch",
        "git repository", "has already been created", "created successfully", "starter structure",
        "approve proposed structure", "repository is ready", "structure is ready"
    )
    return any(k in lower for k in keywords)


def insert_structure_tree(text: str, tree_markdown: Optional[str], commands_markdown: Optional[str] = None) -> str:
    """Replaces the synthesizer's placeholders with the code-rendered tree and commands."""
    if tree_markdown:
        if STRUCTURE_PLACEHOLDER in text:
            text = text.replace(STRUCTURE_PLACEHOLDER, tree_markdown, 1).replace(STRUCTURE_PLACEHOLDER, "")
        elif "\n---" in text:
            parts = text.rsplit("\n---", 1)
            text = parts[0].rstrip() + "\n\n" + tree_markdown + "\n\n---" + parts[1]
        else:
            text = text.rstrip() + "\n\n" + tree_markdown
    else:
        text = text.replace(STRUCTURE_PLACEHOLDER, "")

    if commands_markdown:
        if COMMANDS_PLACEHOLDER in text:
            text = text.replace(COMMANDS_PLACEHOLDER, commands_markdown, 1).replace(COMMANDS_PLACEHOLDER, "")
        else:
            if "\n---" in text:
                parts = text.rsplit("\n---", 1)
                text = parts[0].rstrip() + "\n\n" + commands_markdown + "\n\n---" + parts[1]
            else:
                text = text.rstrip() + "\n\n" + commands_markdown
    else:
        text = text.replace(COMMANDS_PLACEHOLDER, "")

    return text

DEFAULT_HISTORY_TOKEN_BUDGET = 60_000


def history_token_budget() -> int:
    """
    CHAT_HISTORY_TOKEN_BUDGET: how much of the conversation (in tokens) every agent receives. The default fits the MAQ AI
    model (131,072-token context) next to the prompts, the stored PRD/TRD/structure and the answer, so a whole project
    chat is one session for the model. Groq (8,000 tokens per request) gets the newest part that fits (llm_client).
    """
    try:
        return max(2_000, int(os.getenv("CHAT_HISTORY_TOKEN_BUDGET") or DEFAULT_HISTORY_TOKEN_BUDGET))
    except ValueError:
        return DEFAULT_HISTORY_TOKEN_BUDGET


def history_within_budget(messages: List[Message], budget: Optional[int] = None) -> List[Message]:
    """The newest messages (in order) whose total size fits the budget; only very long projects lose their oldest
    messages. The stored PRD/TRD are always added separately, so nothing decided is ever lost."""
    budget = history_token_budget() if budget is None else budget
    kept: List[Message] = []
    total = 0
    for message in reversed(messages):
        size = estimate_request_tokens([message], None)
        if kept and total + size > budget:
            logger.info(f"[History] Conversation longer than {budget} tokens: sending the newest {len(kept)} of "
                        f"{len(messages)} messages")
            break
        kept.append(message)
        total += size
    return list(reversed(kept))


def compact_history_message(role: str, text: str, max_chars: Optional[int] = None) -> Optional[str]:
    """
    A past message for the LLM context: kept in full (the whole project chat is one session for the model; the size is
    limited by history_within_budget, never by cutting messages). Transient framework / provider error messages are
    dropped so they don't pollute later turns. `max_chars` truncates only when explicitly given.
    """
    if not text:
        return None

    # Filter out infrastructure error logs from LLM context
    if ("Error communicating with Agent Framework:" in text or "All LLM providers failed:" in text
            or "(Reference: " in text):
        return None

    if max_chars is None or len(text) <= max_chars:
        return text

    half = max_chars // 2
    return (
        f"{text[:half].rstrip()}\n\n"
        f"[... content truncated for brevity; refer to workflow state ...]\n\n"
        f"{text[-half:].lstrip()}"
    )

async def _execute_agent_pipeline(
    client: OpenAIChatCompletionClient,
    deployment: str,
    user_query: str,
    history: List[Dict],
    conversation_id: Optional[str] = None,
) -> str:
    """Executes the Microsoft Agent Framework multi-agent pipeline with the given client."""
    try:
        client = client or get_agent_framework_client()
        deployment = getattr(client, "current_model", None) or getattr(client, "model", None) or deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
        provider_name = getattr(client, "current_provider_name", "Primary")

        main_agent_prompt = """
You are DevFoundry, an AI-powered software development orchestration assistant.

For every user message you decide ONE thing: answer the user directly, or delegate exactly ONE operation to ONE child agent.
You return a structured decision (reasoning, action, agent, operation, reply, title). You never write PRDs, TRDs or proposals yourself.

AVAILABLE CHILD AGENTS AND OPERATIONS:
1. Requirements_Agent
   - GENERATE_PRD: create the Product Requirements Document from a project description, an uploaded document, or answers to clarifying questions.
   - UPDATE_PRD: change functional requirements (features, users, roles, flows, goals) of an existing PRD.
   - GENERATE_TRD: create the Technical Requirements Document from the PRD.
   - UPDATE_TRD: change, add, remove or confirm technologies in an existing TRD.
2. Repo_Setup_Agent
   - GENERATE_PROJECT_PROPOSAL / UPDATE_PROJECT_PROPOSAL: propose / change the Azure DevOps project name and description (before it exists). Azure DevOps project names must use ONLY letters, numbers, spaces, and hyphens (max 64 chars); never use '&' or special characters, always write 'and'.
   - CREATE_PROJECT: create the Azure DevOps project (explicit approval only); it also suggests a repository name.
   - UPDATE_ADO_PROJECT: rename / change the description of the already created Azure DevOps project.
   - GENERATE_REPOSITORY_PROPOSAL / UPDATE_REPOSITORY_PROPOSAL: propose / rename the repository.
   - CREATE_REPOSITORY: create the empty repository (explicit approval only); it then generates the folder structure by
     running the stack's official generator commands (e.g. create-vite, uv init) and shows it.
   - GENERATE_REPOSITORY_STRUCTURE: generate the structure again with different generator commands (the user does not accept
     the generated approach, or wants another generator / template).
   - UPDATE_REPOSITORY_STRUCTURE: add / change / remove the files the user explicitly asks for on top of the generated
     structure, or bring the structure in line with the latest TRD (STRUCTURE_OUTDATED).
   - PUSH_REPOSITORY_STRUCTURE: push the approved structure to the main branch (explicit approval only); if it was pushed
     before, only the changes are pushed as a follow-up commit.
3. Backlog_Agent (Azure Boards work items; only once the Azure DevOps project exists, see allowed_operations)
   - GENERATE_WORK_ITEMS_PROPOSAL: draft the backlog from the PRD (epic, features, one story per requirement with
     acceptance criteria), or redraft it after the PRD changed (WORK_ITEMS_OUTDATED, "update the work items").
   - UPDATE_WORK_ITEMS_PROPOSAL: change the draft (rename/merge/split features, reword stories, acceptance criteria).
   - CREATE_WORK_ITEMS: create / sync the drafted work items in Azure Boards (explicit approval only, e.g. "the work
     items look good, create them", "yes, create them in Azure Boards").

WORKFLOW STAGES (in order):
REQUIREMENTS -> PRD_REVIEW -> TRD_REVIEW -> PROJECT_REVIEW -> REPOSITORY_REVIEW -> STRUCTURE_REVIEW -> COMPLETE
The system appends a "[Current Workflow State (database)]" block to the user's latest message with current_stage and
allowed_operations. It is authoritative. You may ONLY delegate an operation listed in allowed_operations.

ROUTING RULES:
1. Never skip a step. Moving to the next step requires the user to EXPLICITLY ask for that next step.
   - PRD -> TRD: delegate GENERATE_TRD only if the user explicitly asks for the TRD (e.g. "create the TRD",
     "go ahead with the technical requirements", or "yes" as a direct answer to the question "Would you like me to create a TRD?").
     Approving or praising the PRD ("the PRD looks good", "PRD approved") is NOT a request for the TRD:
     respond, confirming the PRD is approved, and ask whether you should create the TRD now.
   - TRD -> project setup: delegate GENERATE_PROJECT_PROPOSAL only if the user explicitly asks to proceed with project /
     Azure DevOps project setup (e.g. "let's proceed with project setup", "proceed with repository setup", "set up the ADO project").
     Approving the TRD alone ("the TRD looks good") is NOT that request: respond, confirming the TRD is final, and ask
     whether to proceed with project setup.
2. The PRD and TRD can be updated at ANY stage, even after the Azure DevOps project, repository or push exist.
   - Any message about technologies (use, add, replace, remove or keep a language, framework, database, library or cloud
     service) is UPDATE_TRD, EVEN IF the TRD already contains that technology. Never treat it as approval to move on.
   - Changes to features, users, roles or flows -> UPDATE_PRD.
   Updating one document never updates the next one automatically: the state block then lists "outdated" items and the
   user is offered to update them.
   - If your previous reply asked the user to confirm a technology change (it listed fit scores, pros and cons), a
     confirmation ("yes", "switch anyway", "go ahead with it") is UPDATE_TRD (user_intent change_request); declining ("no",
     "keep the current one") is respond: confirm that nothing changed.
3. Outdated items (see "outdated" in the state block) are updated only when the user asks for it:
   - "update the TRD accordingly" (TRD_OUTDATED) -> UPDATE_TRD.
   - project name/description (PROJECT_PROPOSAL_OUTDATED, before creation) -> UPDATE_PROJECT_PROPOSAL;
     (ADO_PROJECT_OUTDATED, after creation, or any rename request) -> UPDATE_ADO_PROJECT.
   - repository structure (STRUCTURE_OUTDATED) -> UPDATE_REPOSITORY_STRUCTURE; pushing the updated structure again -> PUSH_REPOSITORY_STRUCTURE.
4. A project description, an uploaded document, answers to clarifying questions, or a request to proceed with assumptions
   ("make reasonable assumptions", "just generate it") -> GENERATE_PRD (no PRD yet) or UPDATE_PRD. Even a vague one-line
   description goes to GENERATE_PRD: the Requirements_Agent decides whether it needs to ask questions first.
5. Project / repository / structure steps:
   - PROJECT_REVIEW: name/description changes -> UPDATE_PROJECT_PROPOSAL; explicit approval to create -> CREATE_PROJECT.
   - REPOSITORY_REVIEW: different repository name -> UPDATE_REPOSITORY_PROPOSAL; explicit approval to create -> CREATE_REPOSITORY;
     no repository name proposed yet -> GENERATE_REPOSITORY_PROPOSAL.
   - STRUCTURE_REVIEW: changes to folders/files -> UPDATE_REPOSITORY_STRUCTURE; rejecting the generated approach without
     naming a technology ("start over", "use another generator") -> GENERATE_REPOSITORY_STRUCTURE; a different language,
     framework or library is a technology change -> UPDATE_TRD (rule 2), after which the structure is offered for update;
     explicit approval to push (optionally with a commit message) -> PUSH_REPOSITORY_STRUCTURE. Only a message that
     asks to PUSH (or commit) is a push request; "approved" / "looks good" / "create the repo" alone is not - respond.
     An approval confirms only the step it answers; it never moves two steps.
6. COMPLETE: the structure was pushed. Further structure changes -> UPDATE_REPOSITORY_STRUCTURE (pushed later as a follow-up
   commit after explicit approval); project rename/description -> UPDATE_ADO_PROJECT; otherwise respond directly.
7. Never "re-push" when nothing changed: if the user asks to push again and the state shows no unpushed changes, respond.
7b. Work items / backlog / user stories / Azure Boards / sprint planning requests -> Backlog_Agent (rule 3 of the
   agents list). They never change the workflow stage, so they can be used in any stage once the ADO project exists.
   Changes to the work items DRAFT are UPDATE_WORK_ITEMS_PROPOSAL, not UPDATE_PRD; a change to the product's features
   is still UPDATE_PRD (the draft is then flagged as outdated).
8. Greetings, general questions, status questions, or a request to skip/cancel a step -> respond directly.
   - A request to see the current PRD or TRD ("where is the PRD?", "show me the TRD") -> respond with show_document
     "PRD" or "TRD". The system shows the stored document; NEVER write a PRD or TRD yourself in reply.
   - One chat holds ONE project. If the state block shows that this chat already has a PRD and the user describes a
     DIFFERENT product (not a change to the current one), set different_product to true (the system then asks whether
     to start a New Project or replace this PRD). Only if the user explicitly asks to replace the PRD here ->
     GENERATE_PRD when allowed, otherwise UPDATE_PRD (different_product false).
     After that question ("Shall I continue with the current project?"), "yes" / "continue" means the CURRENT project:
     different_product is false and the rest of the message is acted on for the current project (e.g. "yes, create
     the TRD" -> GENERATE_TRD; a bare "yes" -> respond, confirming you continue and offering its next step).
9. If you cannot tell whether the user is approving, asking for a change, or asking for the next step, respond with ONE short
   clarifying question instead of delegating.

DECISION FIELDS:
- reasoning: one short sentence (for logs).
- user_intent: classify the latest user message on its own (approve_only, explicit_next_step, change_request, question, other).
  "The PRD/TRD looks good" or "approved" is approve_only. "yes" in direct answer to an offer of the next step is explicit_next_step.
  An instruction such as "Use PostgreSQL as the database" is change_request even if the TRD already uses it.
- mentions_technology: true if the message names a language, framework, database, library or cloud/infrastructure service.
- action: "delegate" or "respond".
- agent + operation: required when delegating (operation must be in allowed_operations).
- reply: required when responding. Friendly, concise Markdown that ends with the clear next step the user can take.
- show_document: "PRD" or "TRD" only when the user asks to see that document (rule 8); otherwise null.
- different_product: true only for a different product described in a chat that already has a PRD (rule 8).
- title: the name of the PRODUCT the user wants to build, in 2-5 words, based on the whole conversation (e.g. "School Management System", "Clinic Appointment Booking"). Null when no specific product has been described yet: greetings, small talk and questions about you ("what can you do?", "what are you up to?", "what kind of name is DevFoundry?") never produce a title. Never the user's own words, never a question, never a placeholder.
- title_confidence: 0.0-1.0, how clearly the conversation describes ONE specific product (0.0 small talk / questions about DevFoundry, 0.3 vague wish, 0.6 a category without purpose, 0.8 a concrete product, 1.0 a detailed description). The chat is only named from 0.7.
"""

        # Small requests (the routing decision, the technology fit check) use the fast order Azure -> Groq -> MAQ AI;
        # the agents that write documents keep `client` (Azure -> MAQ AI -> Groq)
        small_client = (create_fallback_client(order=orchestrator_provider_order())
                        if isinstance(client, FallbackChatCompletionClient) else client)
        orchestrator = Agent(
            name="Orchestrator",
            instructions=main_agent_prompt,
            client=small_client,
        )

        requirements_agent = get_requirements_agent(client)

        # Stored workflow state (Project / Repository tables) for routing and child agents. Never persisted in chat history.
        workflow_state = store.get_workflow_state(conversation_id) if conversation_id else None
        stage = effective_stage(workflow_state)
        state_note = "\n\n" + routing_context(workflow_state)

        # Convert history into Agent Framework Messages with compacting & error filtering
        def to_messages(items: List[Dict]) -> List[Message]:
            converted = []
            for msg in items:
                role = msg.get("role", "user")
                compact_text = compact_history_message(role, msg.get("text", ""))
                if compact_text:
                    converted.append(Message(role=role, contents=[compact_text]))
            return converted

        new_msg = Message(role="user", contents=[user_query])
        # The whole project chat is one session for the model, limited only by the token budget (no window)
        message_list = history_within_budget(to_messages(history)) + [new_msg]

        # 1. Execute the Orchestrator: a structured routing decision (Agent Framework response_format)
        logger.info(f"[Orchestrator] Running Orchestrator Agent (Provider: {provider_name}, Model: {deployment}, History: {len(history)} messages, Stage: {stage})...")
        orch_start = time.time()
        # Orchestrator: the whole conversation (within the token budget) + latest query with state
        recent_orch_history = message_list[:-1]
        orchestrator_messages = recent_orch_history + [Message(role="user", contents=[user_query + state_note])]
        progress.step("Deciding the next step")
        orchestrator_response = await orchestrator.run(
            messages=orchestrator_messages,
            options={"response_format": RoutingDecision}
        )
        orch_duration = round((time.time() - orch_start) * 1000, 1)
        decision = parse_routing_decision(orchestrator_response)
        logger.info(f"[Orchestrator] Completed in {orch_duration}ms -> {decision.action} "
                    f"{decision.agent or ''} {decision.operation or ''} | intent={decision.user_intent}, "
                    f"tech={decision.mentions_technology} | {decision.reasoning}")

        # Emit a title prefix when the model is confident the chat describes a product and no ADO project exists yet
        has_official_project = bool(((workflow_state or {}).get("project") or {}).get("ado_project_id"))
        title = None if has_official_project else inferred_title(decision)
        logger.info(f"[Title] suggestion={decision.title!r} confidence={decision.title_confidence} "
                    f"-> {'used' if title else 'ignored'} (threshold {title_confidence_threshold()})")
        title_prefix = f"TITLE: {title}\n\n" if title else ""

        # The user is answering the "different product" question: their answer decides ("yes, create the TRD" means the
        # current project), so the earlier product description in the history must not trigger the question again.
        if answers_different_product_question(history):
            if decision.different_product:
                logger.info("[Routing Guard] Answer to the different-product question: continuing with the current project")
                decision.different_product = False
            if decision.operation not in ("GENERATE_PRD", "UPDATE_PRD") and len(history) >= 2:
                # Continuing with the current project: the other product's description and the question are not part
                # of it, so the child agents must not see them (seen live: the TRD agent asked which product to use).
                message_list = history_within_budget(to_messages(history[:-2])) + [new_msg]

        # "yes, switch anyway" after a technology-change confirmation question is always a TRD update
        if (tech_fit.is_confirmation_turn(history) and tech_fit.looks_affirmative(user_query)
                and decision.operation != "UPDATE_TRD" and "UPDATE_TRD" in allowed_operations(stage, workflow_state)):
            logger.info("[Routing Guard] Confirmation of the technology change -> UPDATE_TRD")
            decision = RoutingDecision(reasoning="user confirmed the warned technology change", user_intent="change_request",
                                       mentions_technology=True, action="delegate", agent="Requirements_Agent",
                                       operation="UPDATE_TRD")

        # 2. Workflow guard: the operation must be allowed in the current stage
        corrected = apply_intent_rules(decision, stage, workflow_state)
        if corrected is not decision:
            logger.warning(f"[Routing Guard] {decision.action} {decision.operation or ''} -> {corrected.action} "
                           f"{corrected.operation or ''} ({corrected.reasoning})")
            decision = corrected
        blocked = validate_decision(decision, stage, workflow_state)
        if blocked:
            logger.warning(f"[Routing Guard] {decision.operation} is not allowed in stage {stage}; answering directly")
            decision = RoutingDecision(reasoning="blocked by workflow guard", action="respond",
                                       reply=blocked_reply(stage, blocked))
        # A push needs an explicit push request about a structure the user has seen; otherwise show it first
        if decision.action == "delegate" and decision.operation == "PUSH_REPOSITORY_STRUCTURE":
            review = push_review_reply(user_query, history, workflow_state)
            if review:
                logger.warning("[Routing Guard] PUSH_REPOSITORY_STRUCTURE needs an explicit push request after the "
                               "structure was shown; showing the structure instead")
                decision = RoutingDecision(reasoning="push needs review", user_intent=decision.user_intent,
                                           action="respond", reply=review)
        route_agent = decision.agent if decision.action == "delegate" else None
        operation = decision.operation
        response_text = decision.reply or blocked_reply(stage, "")

        # Technology-change fit check: a dedicated step BEFORE any TRD update (threshold checked in code, question
        # written by code), so a clearly worse choice is never applied without the user's explicit confirmation.
        confirmed_request: Optional[str] = None
        fit_checked = False
        if route_agent == "Requirements_Agent" and operation == "UPDATE_TRD":
            fit_checked = True
            progress.step("Checking how well the technology fits this project")
            gate_reply, confirmed_request = await tech_fit.gate(small_client, user_query, history, workflow_state or {})
            if gate_reply:
                route_agent = None                      # answered directly below; the TRD is not touched
                response_text = gate_reply
                decision.show_document = None

        if route_agent == "Requirements_Agent":
            logger.info(f"[Routing Decision] -> Delegating to Requirements_Agent ({operation})")

            workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {operation}]"
            if workflow_state is not None and workflow_state.get("project"):
                include_trd = operation in ("GENERATE_TRD", "UPDATE_TRD")
                workflow_context_note += "\n\n" + store.describe_workflow_state(
                    workflow_state, include_prd=True, include_trd=include_trd
                )
            if operation == "GENERATE_PRD" and asked_to_proceed(user_query, history):
                workflow_context_note += (
                    "\n[Proceed on assumptions] The user explicitly asked you to make reasonable assumptions: write the "
                    "PRD now (status COMPLETE), mark gaps \"assumed\" in readiness, list every assumption in "
                    "\"Assumptions\", and do NOT ask clarifying questions.")
            if confirmed_request:
                workflow_context_note += (
                    "\n[Confirmed technology change] After a fit warning, the user explicitly confirmed this change: "
                    f"\"{confirmed_request.strip()[:500]}\". Apply it now, set tech_change_assessment.user_confirmed to "
                    "true, and write the rationale honestly, naming the trade-off the user accepted.")

            # Requirements_Agent: the whole conversation (token budget) + the latest message with its workflow context
            recent_req_history = message_list[:-1]
            context_user_msg = Message(role="user", contents=[user_query + workflow_context_note])
            req_messages = recent_req_history + [context_user_msg]

            # 1. Requirements Agent Input Guardrail Check
            # Only the user's own words are checked: the appended workflow context (stored PRD/TRD) is system data
            req_input_check = await check_requirements_agent_input(user_query)
            if not req_input_check.allowed:
                logger.warning(
                    f"[Requirements_Agent Guardrail Block] Input blocked by '{req_input_check.guardrail_name}': {req_input_check.blocked_reason}"
                )
                child_raw_json = json.dumps({
                    "workflow_stage": "PRD",
                    "status": "CLARIFICATION_REQUIRED",
                    "clarification_questions": [{
                        "question": guardrail_block_message(req_input_check),
                        "choices": [],
                        "reason": req_input_check.blocked_reason or "Guardrail violation"
                    }],
                    "known_so_far": "Request flagged by requirements security guardrail.",
                    "readiness_check": {}
                })
            else:
                # Invoke Requirements Child Agent
                logger.info(f"[Requirements_Agent] Processing request with operation: {operation}...")
                child_start = time.time()
                progress.step(OPERATION_STEPS.get(operation, "Working on your request"))
                child_response = await requirements_agent.run(messages=req_messages)
                child_duration = round((time.time() - child_start) * 1000, 1)
                child_raw_json = child_response.messages[-1].text if child_response.messages else ""
                logger.info(f"[Requirements_Agent] Completed in {child_duration}ms")

                # 2. Requirements Agent Output Guardrail Check
                req_output_check = await check_requirements_agent_output(child_raw_json)
                if not req_output_check.allowed:
                    logger.warning(
                        f"[Requirements_Agent Guardrail Block] Output blocked by '{req_output_check.guardrail_name}': {req_output_check.blocked_reason}"
                    )
                    child_raw_json = json.dumps({
                        "workflow_stage": "PRD",
                        "status": "CLARIFICATION_REQUIRED",
                        "clarification_questions": [{
                            "question": "The generated requirements document could not be validated by internal safety checks. Would you like to refine the specification?",
                            "choices": [],
                            "reason": req_output_check.blocked_reason or "Output guardrail violation"
                        }],
                        "known_so_far": "Requirements generation interrupted by guardrail.",
                        "readiness_check": {}
                    })

            # Readiness guard: a new PRD is only accepted when the agent's own readiness check has no missing item
            # ...unless the user explicitly asked to proceed on assumptions (then gaps go to "Assumptions", not questions)
            missing = (missing_readiness_items(child_raw_json)
                       if operation == "GENERATE_PRD" and not asked_to_proceed(user_query, history) else [])
            if missing:
                logger.warning(f"[Readiness Guard] PRD written although {', '.join(missing)} missing; asking again")
                retry_messages = req_messages + [
                    Message(role="assistant", contents=[child_raw_json]),
                    Message(role="user", contents=[
                        f"[Readiness guard] Your readiness check marks {', '.join(missing)} as missing, so the PRD must not be "
                        "written yet. Return CLARIFICATION_REQUIRED asking only for the missing REQUIRED items."]),
                ]
                child_response = await requirements_agent.run(messages=retry_messages)
                child_raw_json = child_response.messages[-1].text if child_response.messages else child_raw_json

            # JSON guard: unreadable output would be shown to the user but never saved (seen with fallback models).
            # Ask once for the same answer as valid JSON (safe: the Requirements Agent has no tools).
            json_error = agent_json_error(child_raw_json)
            if json_error:
                logger.warning(f"[JSON Guard] Requirements_Agent output is not valid JSON ({json_error}); asking again")
                repair_messages = req_messages + [
                    Message(role="assistant", contents=[child_raw_json]),
                    Message(role="user", contents=[
                        f"[JSON guard] Your previous answer is not valid JSON ({json_error}). Return the same answer again "
                        "as ONE valid JSON object only, exactly following the output contract. No text outside the JSON."]),
                ]
                child_response = await requirements_agent.run(messages=repair_messages)
                repaired = child_response.messages[-1].text if child_response.messages else ""
                if not agent_json_error(repaired):
                    child_raw_json = repaired
                else:
                    logger.error("[JSON Guard] Requirements_Agent output still not valid JSON; nothing was saved")

            # Second line of defence (the agent's own assessment), only when the dedicated fit check did not run
            if operation == "UPDATE_TRD" and not fit_checked:
                assessment = tech_change_needing_confirmation(child_raw_json)
                if assessment:
                    logger.info(f"[Tech Change Check] '{assessment.get('requested_change')}' fits worse "
                                f"({assessment.get('requested_fit')} vs {assessment.get('current_fit')}); asking the user to confirm")
                    child_raw_json = json.dumps({"status": "CONFIRMATION_REQUIRED", "workflow_stage": "TRD",
                                                 "tech_change_assessment": assessment})

            saved = persist_requirements_output(conversation_id, child_raw_json)
            child_status = (parse_agent_json(child_raw_json) or {}).get("status")
            # Only a SAVED document is ever shown (seen live: a COBOL TRD was displayed but never saved)
            unsaved_document = (not saved and conversation_id is not None
                                and child_status not in ("CLARIFICATION_REQUIRED", "CONFIRMATION_REQUIRED"))
            if unsaved_document:
                logger.error(f"[ProjectStore] {operation} output was not saved (status={child_status!r}); "
                             "not showing it")
            follow_ups = follow_up_note(conversation_id)

            # 3. Follow-Up Flow: Child Agent -> Orchestrator Synthesizer -> User
            logger.info("[Orchestrator] Converting Requirements_Agent output into user-facing natural language...")
            synthesis_prompt = f"""
You are the DevFoundry Orchestrator. 
The Requirements_Agent has produced the following structured output:
{child_raw_json}

Your task:
Convert this output into a friendly, structured, and professional response for the user:

CASE 1: If status is "CLARIFICATION_REQUIRED":
- Start with one sentence explaining that a few details are needed before the document can be written, then summarize
  "known_so_far" in one short sentence.
- Present the questions as a numbered list in the given order: the question in **bold**; when it has "choices", list them as
  bullets indented under the question. Do not show the "reason" field verbatim; you may use it to phrase the question clearly.
- Closing sentence: "You can answer in one message (for example "1: ..., 2: ..."), or say **"make reasonable assumptions"** and
  I'll draft the document with the assumptions clearly listed."

CASE 1b: If status is "CONFIRMATION_REQUIRED" (tech_change_assessment):
- Say that before changing the TRD you want to double-check: the requested change (requested_change) fits this project less
  well than the current choice. Nothing has been changed yet.
- **Fit for this project:** current choice current_fit/10, requested choice requested_fit/10.
- **Pros of the requested choice** (bulleted list) and **Cons of the requested choice** (bulleted list), from the assessment.
- **Recommendation:** the recommendation sentence.
- Closing sentence: ask whether they still want to switch (they can say e.g. "yes, switch anyway") or keep the current choice.

CASE 2: If status is "COMPLETE" and workflow_stage is "PRD" (or contains structured_project_response):
- Start with ONE introductory sentence before the first heading, e.g. "Here is the suggested **Product Requirements Document
  (PRD)** based on your project description:" (say "based on the uploaded document" if the requirements came from an attached
  file). For an updated PRD (change_notes present) say "Here is the updated, suggested **Product Requirements Document (PRD)**:"
  and then summarize what changed in one short "**What changed:**" paragraph based on change_notes.
- Present the complete PRD with a Markdown heading per section, in this order (skip a section only if it is missing):
  * **Objective**, **Problem Statement** (paragraphs)
  * **Target Users**, **Goals** (bulleted lists)
  * **Functional Requirements** (bulleted list, "**FR-001:** The system shall ...")
  * **User Experience** (paragraph), **User Flow** (numbered list)
  * **Non-Goals**, **Constraints** (bulleted lists; write "None stated" when empty)
  * **Assumptions** (bulleted list; only when not empty, introduced with "These were assumed - please confirm or correct them:")
  * **Open Questions** (bulleted list; only when not empty, introduced with "Still open - you can answer these anytime:")
- Closing sentence: "Would you like me to create the Technical Requirements Document (TRD) next?"

CASE 3: If status is "COMPLETE" and workflow_stage is "TRD" (or contains technical_requirements_document):
- Start with ONE introductory sentence before the first heading, e.g. "Here is the suggested **Technical Requirements Document
  (TRD)** based on the approved PRD:" (or "Here is the updated, suggested **Technical Requirements Document (TRD)**:" when
  change_notes is present).
- Present the comprehensive technical stack recommendations using clean Markdown headings:
  * **Frontend Technology Stack** (Framework, Supporting Technologies, Tools & Libraries, Rationale)
  * **Backend Technology Stack** (Language, Framework, API Technologies, Supporting Technologies, Rationale)
  * **Database Technology Stack** (Database, Database Type, Supporting Technologies, Rationale)
  * **Integration & Infrastructure** (External Integrations, File Storage, Messaging, Deployment, CI/CD & Observability, Rationale)
  * **Security** (Authentication & Authorization, Data Protection, Application Security, Compliance, Rationale)
- Under each heading use ONE flat bullet per field, e.g. "- **Framework:** React" and "- **Supporting Technologies:** Redux Toolkit, React Router".
  Do not nest lists. Never output "(mandated by user)", "(mandated)", or "(as requested)" anywhere in the stack. Output only clean technology names.
  Put the rationale in its own short 2-sentence paragraph starting with "**Rationale:**" (strictly 2 sentences, under 45 words, project-specific, zero generic marketing praise or velocity filler).
- If change_notes is present, start with a short "**What changed:**" paragraph based on change_notes (including when nothing needed to change).
- Closing sentence: ask whether they want to change any of these technology choices or proceed with project setup.

CRITICAL RULE — ZERO META-COMMENTARY:
- Output ONLY the final user-facing response.
- NEVER include internal explanations, checklists, rule reviews, or meta-commentary (e.g., NEVER write "That's the formatted response for CASE...", "Here's a quick checklist of what was included and why", or tables detailing fields and purposes).
- All project and repository URLs in Markdown links must have spaces encoded with %20 (e.g. "[Project Name](https://dev.azure.com/org/Project%20Name)") so they are clickable links.
- Stop immediately after the single closing sentence following the "---" divider.

CLOSING RULE (every case): the content ends, then a divider line "---" on its own line, then exactly ONE plain sentence (no
bullet, no heading, no bold label) telling the user what they can do next. Nothing after that sentence.
Do NOT output raw JSON. Output only clean, beautifully formatted Markdown text.
Put a blank line before and after every heading, divider (---) and list; never put "---" at the end of a text line.
{follow_ups}
"""
            synth_start = time.time()
            synthesizer_agent = Agent(
                name="Orchestrator_Synthesizer",
                instructions=synthesis_prompt,
                client=client,
            )
            if unsaved_document:
                response_text = document_not_saved_message(operation)
            else:
                progress.step("Preparing the reply")
                synthesis_result = await synthesizer_agent.run(
                    messages=[Message(role="user", contents=["Please present this update to the user."])])
                synth_duration = round((time.time() - synth_start) * 1000, 1)
                response_text = synthesis_result.messages[-1].text if synthesis_result.messages else child_raw_json
                response_text = enforce_closing_line(response_text)
                logger.info(f"[Orchestrator] Response synthesized ({synth_duration}ms)")
                if saved and child_status == "COMPLETE" and not presents_document(response_text):
                    # Seen live: the model answered "Please share the update you'd like me to present..." although
                    # the TRD was saved. The user always sees the saved document.
                    document = "TRD" if (parse_agent_json(child_raw_json) or {}).get("technical_requirements_document") else "PRD"
                    logger.warning(f"[Orchestrator] The reply did not present the saved {document}; showing it from "
                                   "the database")
                    response_text = (stored_document_reply(document, store.get_workflow_state(conversation_id))
                                     or response_text)

        elif route_agent == "Repo_Setup_Agent":
            logger.info(f"[Routing Decision] -> Delegating to Repo_Setup_Agent ({operation})")

            # Repo_Setup_Agent only needs TRD architecture and folder structure (not the bulky PRD)
            workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {operation}]"
            if workflow_state is not None:
                workflow_context_note += "\n\n" + store.describe_workflow_state(
                    workflow_state, include_prd=operation in SCAFFOLD_OPERATIONS, include_trd=True,
                    include_structure=True
                )
            if operation in SCAFFOLD_OPERATIONS:
                workflow_context_note += "\n" + describe_toolchains()

            # Repo_Setup_Agent: the whole conversation (token budget) + the latest message with its workflow context
            recent_repo_history = message_list[:-1]
            context_user_msg = Message(role="user", contents=[user_query + workflow_context_note])
            repo_messages = recent_repo_history + [context_user_msg]

            # Invoke Repo Setup Child Agent
            logger.info(f"[Repo_Setup_Agent] Processing request with operation: {operation}...")
            child_start = time.time()
            # Built per operation: only the tools this operation allows (none for proposals)
            repo_setup_agent = get_repo_setup_agent(client, conversation_id, operation)
            progress.step(OPERATION_STEPS.get(operation, "Working on your request"))
            child_response = await repo_setup_agent.run(messages=repo_messages)
            child_duration = round((time.time() - child_start) * 1000, 1)
            child_raw_json = child_response.messages[-1].text if child_response.messages else ""
            logger.info(f"[Repo_Setup_Agent] Completed in {child_duration}ms")
            persist_repo_setup_output(conversation_id, child_raw_json)
            follow_ups = follow_up_note(conversation_id)

            # The structure shown to the user is rendered from the stored files (the same files that get pushed)
            child_data = parse_agent_json(child_raw_json) or {}
            structure_tree = None
            commands_block = None
            if conversation_id and (child_data.get("workflow_stage") in STRUCTURE_STAGES or
                                    operation in ("CREATE_REPOSITORY", "GENERATE_REPOSITORY_STRUCTURE", "UPDATE_REPOSITORY_STRUCTURE")):
                wf_state = store.get_workflow_state(conversation_id)
                structure_tree = render_structure_for_display(wf_state)

            # Orchestrator Synthesizer for Repo_Setup_Agent
            logger.info("[Orchestrator] Converting Repo_Setup_Agent output into user-facing natural language...")
            synthesis_prompt = f"""
You are the DevFoundry Orchestrator.
The Repo_Setup_Agent has produced the following structured output:
{child_raw_json}

Your task:
Convert this output into a friendly, structured, and professional response for the user:

CASE 1: If status is "ERROR":
- Clearly and politely explain what went wrong based on the error details (do NOT expose any secrets or tokens).
- Acknowledge that the saved proposal or repository structure remains saved.
- Guide the user on what corrective steps are needed or offer to retry.

CASE 2: If status is "COMPLETE" and workflow_stage is "PROJECT_REVIEW" (or contains project_proposal):
- Output exactly two headings and their content, nothing else above or between them (no "Proposed Azure DevOps Project" title,
  no extra explanation lines):
  "### Proposed Project Name" followed by the name on its own line, then
  "### Project Description" followed by the description paragraph.
- Closing sentence, e.g.: "Please review the name and description and tell me what to change, or approve them to create the
  Azure DevOps project."

CASE 3: If status is "COMPLETE" and workflow_stage is "PROJECT_CREATED":
- Announce that the Azure DevOps project has been created successfully!
- Show the project details clearly:
  * **Project Name**: [name]
  * **Project ID**: [id]
  * **Project URL**: [[name]](<url_with_%20_for_spaces>) (ensure the markdown link URL is encoded so it is clickable in browsers)
- Present the next step: the suggested repository name (**Suggested Repository Name**: repository_proposal.repo_name).
- Closing sentence: "Shall I create the repository with this name, or would you prefer a different name?"

CASE 4: If status is "COMPLETE" and workflow_stage is "REPOSITORY_REVIEW":
- Present the **Proposed Repository Name** (repository_proposal.repo_name).
- Conclude by asking whether to create the repository with this name or use a different name.

CASE 5: If status is "COMPLETE" and workflow_stage is "REPOSITORY_CREATED":
- Announce that the (empty) Azure DevOps Git repository has been created successfully, with **Repository Name**, **Repository ID** and **Repository URL** ([link](url)).
- Explain in one or two sentences that the starter structure was generated with the stack's official tools (structure.summary,
  in plain words - do NOT mention or list any commands), then output the placeholder line (see STRUCTURE RULE).
- Closing sentence: they can approve it to push it to the main branch, ask for extra files/folders, or ask for a different stack.

CASE 6: If status is "COMPLETE" and workflow_stage is "STRUCTURE_REVIEW":
- Briefly state what changed (structure.summary), then output the placeholder line (see STRUCTURE RULE).
- Invite the user to request further changes or approve pushing it to the main branch.

CASE 6b: If status is "COMPLETE" and workflow_stage is "STRUCTURE_OPTIONS":
- Explain that the requested stack (requested_stack) cannot be generated on the DevFoundry server right now (reason), so
  nothing was generated yet (if a repository was just created, say it exists and is still empty).
- List the options as a numbered list: **label** — description in plain words (do not show commands).
- Closing sentence: ask the user to choose an option (or name another stack). Do NOT output the structure placeholder line.

CASE 7: If status is "COMPLETE" and workflow_stage is "STRUCTURE_PUSHED":
- Start with heading "### Structure Pushed Successfully" followed by:
  "The approved structure has been pushed to the repository. Here's what was committed:"
- Immediately output the placeholder line: {STRUCTURE_PLACEHOLDER}
- Next line: "Repository setup is complete, and the project is ready for development."
- Next, list each commit from push.commits as exactly one clean bullet line (do NOT list individual added, edited, or deleted file paths, as the directory tree already shows them):
  "- **<kind>** — `<short commit_id>` — *<commit_message>* — <files_count> files — [View Commit](<commit_url>)"
- Next, show the details table:
  | Detail | Value |
  |---|---|
  | **Repository** | <repository_name> |
  | **Branch** | <branch> |
  | **Files** | <files_count> |

CASE 8: If status is "COMPLETE" and workflow_stage is "ADO_PROJECT_UPDATED":
- Announce that the Azure DevOps project was updated, with **Project Name**, **Project Description** and **Project URL** ([link](url)).

STRUCTURE RULE (CASES 1, 5, 6, 7):
- Output the exact line {STRUCTURE_PLACEHOLDER} on its own line where the folder structure belongs.
- The system replaces this placeholder with the exact stored structure and the generator commands under it.
- NEVER draw the folder tree, list file paths or repeat any generator commands yourself.

CRITICAL RULE — ZERO META-COMMENTARY:
- Output ONLY the final user-facing response.
- NEVER include internal explanations, checklists, rule reviews, or meta-commentary (e.g., NEVER write "That's the formatted response for CASE...", "Here's a quick checklist of what was included and why", or tables detailing fields and purposes).
- All project and repository URLs in Markdown links must have spaces encoded with %20 (e.g. "[Project Name](https://dev.azure.com/org/Project%20Name)") so they are clickable links.
- Stop immediately after the single closing sentence following the "---" divider.

CLOSING RULE (every case): the content ends, then a divider line "---" on its own line, then exactly ONE plain sentence (no
bullet, no heading, no bold label) telling the user what they can do next. Nothing after that sentence.
Do NOT output raw JSON. Output only clean, beautifully formatted Markdown text.
Put a blank line before and after every heading, divider (---) and list; never put "---" at the end of a text line.
{follow_ups}
"""
            synth_start = time.time()
            synthesizer_agent = Agent(
                name="Orchestrator_Synthesizer",
                instructions=synthesis_prompt,
                client=client,
            )
            progress.step("Preparing the reply")
            synthesis_result = await synthesizer_agent.run(
                messages=[Message(role="user", contents=["Please present this update to the user."])])
            synth_duration = round((time.time() - synth_start) * 1000, 1)
            response_text = synthesis_result.messages[-1].text if synthesis_result.messages else child_raw_json
            response_text = enforce_closing_line(normalize_reply(insert_structure_tree(response_text, structure_tree, commands_block)))
            logger.info(f"[Orchestrator] Response synthesized ({synth_duration}ms)")

        elif route_agent == "Backlog_Agent":
            logger.info(f"[Routing Decision] -> Delegating to Backlog_Agent ({operation})")
            progress.step(OPERATION_STEPS.get(operation, "Working on the work items"))
            response_text = enforce_closing_line(await run_backlog_operation(
                client, operation, user_query, message_list, workflow_state or {}))

        else:
            logger.info("[Routing Decision] -> Handled directly by Orchestrator (Path A Generic/Conversational)")
            stored_document = stored_document_reply(decision.show_document, workflow_state)
            if stored_document:
                logger.info(f"[Routing Decision] Showing the stored {decision.show_document}")
            response_text = enforce_closing_line(stored_document or normalize_reply(response_text))
            if not stored_document and should_attach_structure_tree(response_text, workflow_state):
                tree = render_structure_for_display(workflow_state)
                if tree:
                    logger.info("[Routing Decision] Attaching proposed repository structure to conversational reply")
                    response_text = enforce_closing_line(insert_structure_tree(response_text, tree))

        return title_prefix + response_text

    except Exception as e:
        if "content_filter" in str(e) or type(e).__name__ == "OpenAIContentFilterException":
            # Azure OpenAI content safety (incl. prompt-injection / jailbreak detection) rejected the input
            logger.warning(f"[Agent Pipeline] Blocked by the Azure OpenAI content filter: {type(e).__name__}")
            return ("⚠️ This message (or an attached document) was blocked by the AI content-safety filter, usually because it "
                    "contains text that looks like instructions to the AI (a possible prompt injection). Please remove that text "
                    "and try again. Nothing was changed.")
        reference = uuid.uuid4().hex[:8]
        if isinstance(e, AllProvidersFailedError):
            logger.error(f"[Agent Pipeline] [ref {reference}] No AI model could answer:\n  " + "\n  ".join(e.errors))
            # Seen live: the repository was created and its structure generated, then only the reply step failed and
            # the user was told "Nothing was changed". Report what was really done, from the stored state.
            before = locals().get("workflow_state")
            if conversation_id and before is not None:
                partial = partial_success_reply(before, store.get_workflow_state(conversation_id), reference)
                if partial:
                    logger.warning(f"[Agent Pipeline] [ref {reference}] The reply failed after changes were saved; "
                                   "showing them from the database")
                    return partial
            return SERVICE_UNAVAILABLE_MESSAGE.format(reference=reference)
        logger.exception(f"[Agent Pipeline] [ref {reference}] Unexpected error: {type(e).__name__}: {e}")
        return UNEXPECTED_ERROR_MESSAGE.format(reference=reference)

async def generate_chat_response(user_query: str, history: List[Dict], conversation_id: Optional[str] = None) -> str:
    """Runs the agent pipeline on the tiered fallback client (Azure OpenAI -> Groq models -> streamed MAQ AI)."""
    client = get_agent_framework_client()
    return await _execute_agent_pipeline(client, getattr(client, "current_model", ""), user_query, history, conversation_id)

def get_suggestions(conversation_id: Optional[str]) -> List[Dict[str, str]]:
    """Next-step suggestions (label + prompt) for the chips above the chat input."""
    if not conversation_id:
        return []
    try:
        return next_steps(store.get_workflow_state(conversation_id))
    except Exception as e:
        logger.error(f"[Suggestions] Could not compute suggestions: {e}")
        return []

# What the dashboard shows while an operation runs (progress.step)
OPERATION_STEPS = {
    "GENERATE_PRD": "Writing the PRD", "UPDATE_PRD": "Updating the PRD",
    "GENERATE_TRD": "Writing the TRD", "UPDATE_TRD": "Updating the TRD",
    "GENERATE_PROJECT_PROPOSAL": "Proposing the Azure DevOps project", "UPDATE_PROJECT_PROPOSAL": "Updating the project proposal",
    "CREATE_PROJECT": "Creating the Azure DevOps project", "UPDATE_ADO_PROJECT": "Updating the Azure DevOps project",
    "GENERATE_REPOSITORY_PROPOSAL": "Suggesting a repository name", "UPDATE_REPOSITORY_PROPOSAL": "Updating the repository name",
    "CREATE_REPOSITORY": "Creating the repository and generating its structure",
    "GENERATE_REPOSITORY_STRUCTURE": "Generating the repository structure",
    "UPDATE_REPOSITORY_STRUCTURE": "Updating the repository structure", "PUSH_REPOSITORY_STRUCTURE": "Pushing to Azure DevOps",
    "GENERATE_WORK_ITEMS_PROPOSAL": "Drafting the work items", "UPDATE_WORK_ITEMS_PROPOSAL": "Updating the work items draft",
    "CREATE_WORK_ITEMS": "Creating the work items in Azure Boards",
}

DEFAULT_TITLE = "New Project"
DEFAULT_TITLES = {"new project", "new conversation", "new chat", "untitled", ""}

_GREETING_PATTERN = re.compile(
    r"^[\s!.,?:;\"'`~-]*(hi+|hello+|hey+|heyy+|heya+|hiya+|greetings|good\s+(morning|afternoon|evening|day)|"
    r"howdy|sup|yo|what'?s\s+up|how\s+are\s+you|test|testing)[\s!.,?:;\"'`~-]*$",
    re.IGNORECASE,
)


def is_conversational_or_greeting(text: Optional[str]) -> bool:
    """True if text is empty, a casual greeting, or pleasantry that should never become a project title."""
    val = (text or "").strip()
    if not val:
        return False
    return bool(_GREETING_PATTERN.match(val))


def _title_from_query(user_query: str) -> Optional[str]:
    """The title older versions derived from the first words of the user's message (now only used to recognise and
    replace such titles; a message is never turned into a title any more)."""
    first_line = (user_query.split("[Attached File Content]")[0].strip().split("\n") or [""])[0].strip()
    if is_conversational_or_greeting(first_line):
        return None
    words = first_line.split()
    if len(words) < 2 and len(first_line) < 5:
        return None
    title = " ".join(words[:5])
    if len(title) > 36:
        title = title[:33] + "..."
    return title or None


DEFAULT_TITLE_CONFIDENCE_THRESHOLD = 0.7


def title_confidence_threshold() -> float:
    """How sure the Orchestrator must be that the chat describes a specific product before it names the chat."""
    try:
        return float(os.getenv("TITLE_CONFIDENCE_THRESHOLD") or DEFAULT_TITLE_CONFIDENCE_THRESHOLD)
    except ValueError:
        return DEFAULT_TITLE_CONFIDENCE_THRESHOLD


def inferred_title(decision: Any) -> Optional[str]:
    """The Orchestrator's product name for the chat, only when it is confident enough (never a placeholder)."""
    title = (getattr(decision, "title", None) or "").strip()
    try:
        confidence = float(getattr(decision, "title_confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1:                      # tolerate a 0-10 or 0-100 scale
        confidence = confidence / 10 if confidence <= 10 else confidence / 100
    if not title or store.is_placeholder_name(title) or confidence < title_confidence_threshold():
        return None
    return title


def resolve_conversation_title(current_title: Optional[str], llm_title: Optional[str], first_user_message: str,
                               official_name: Optional[str]) -> str:
    """
    The conversation (sidebar) title:
    1. Once the Azure DevOps project is created (the name the user finalized), the title is ALWAYS that name, and it
       follows every rename of the project.
    2. Before that, the Orchestrator's name for the product (e.g. "School Management System"), which it only returns once
       the chat clearly describes a product (title_confidence >= TITLE_CONFIDENCE_THRESHOLD). It replaces only a
       temporary title: the default, a placeholder, a greeting, or one copied from the first message by older versions.
    3. Otherwise "New Project". The user's own words never become the title.
    """
    if not store.is_placeholder_name(official_name):
        return official_name.strip()

    current = (current_title or "").strip()
    is_transient = (
        not current
        or current.lower() in DEFAULT_TITLES
        or store.is_placeholder_name(current)
        or is_conversational_or_greeting(current)
        or current == _title_from_query(first_user_message or "")
    )
    if not store.is_placeholder_name(llm_title) and is_transient:
        return llm_title.strip()
    return DEFAULT_TITLE if is_transient else current


_conversation_locks: Dict[str, asyncio.Lock] = {}


async def process_chat_request(conversation_id: Optional[str], user_query: str, file: Optional[UploadFile] = None,
                               *, user_id: str) -> ChatResponse:
    """
    Different conversations run in parallel. Requests for the SAME conversation run one after another, because each
    reads the conversation history and writes it back (two at once would lose a message).
    """
    key = str(conversation_id or "").strip()
    if not key or key.lower() in ("null", "none"):
        return await _process_chat_request(conversation_id, user_query, file, user_id=user_id)
    lock = _conversation_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        logger.info(f"[Conversation] '{key}' is busy with another message; this one waits for it")
    async with lock:
        progress.start(key, user_id)             # live progress for the dashboard (GET /api/chat/progress/{id})
        try:
            return await _process_chat_request(conversation_id, user_query, file, user_id=user_id)
        finally:
            progress.finish(key)


async def _process_chat_request(conversation_id: Optional[str], user_query: str, file: Optional[UploadFile] = None,
                                *, user_id: str) -> ChatResponse:
    overall_start = time.time()

    # If conversation_id is not provided, create a new conversation for this initial message
    if not conversation_id or str(conversation_id).strip().lower() in ("null", "none", ""):
        logger.info("[Conversation] No conversation_id provided; creating new conversation row for initial message.")
        new_conv = create_new_conversation(user_id, title="New Project")
        conversation_id = new_conv.conversation_id
        conv = {"conv_id": conversation_id, "title": "New Project", "content": []}
    else:
        # Only the owner can continue a conversation; anyone else gets "not found"
        conv = get_conversation_by_id(conversation_id, user_id)
        if not conv:
            logger.warning(f"[Supabase] Conversation '{conversation_id}' not found in DB.")
            raise ValueError(f"Conversation '{conversation_id}' not found.")

    logger.info(f"=== Starting chat processing pipeline for conversation '{conversation_id}' ===")
    content_array = conv.get("content") or []

    
    # Process document upload if provided
    if file:
        file_info = process_uploaded_document(file)
        if file_info:
            user_query = user_query + f"\n\n[Attached File Content]:\n{file_info}"

    # 1. Orchestrator Input Guardrail Boundary
    progress.step("Checking your message")
    input_check = await check_input_guardrail(user_query)
    if not input_check.allowed:
        logger.warning(
            f"[Orchestrator Guardrail Block] User input blocked by '{input_check.guardrail_name}': {input_check.blocked_reason}"
        )
        friendly_error = guardrail_block_message(input_check)
        content_array.append({"role": "user", "text": user_query})
        content_array.append({"role": "assistant", "text": friendly_error})
        supabase = get_supabase_client()
        try:
            supabase.table("Conversation").update({"content": content_array}).eq("conv_id", conversation_id).execute()
        except Exception as e:
            logger.error(f"[Supabase] Failed to save conversation history on guardrail block: {e}")
        return ChatResponse(
            conversation_id=conversation_id,
            response=friendly_error,
            suggestions=get_suggestions(conversation_id),
        )

    # Generate response via multi-agent pipeline
    response_text = await generate_chat_response(user_query, content_array, conversation_id)

    # 2. Orchestrator Output Guardrail Boundary (redacts PII / blocks restricted URLs)
    progress.step("Final safety check")
    output_check = await check_output_guardrail(response_text)
    if not output_check.allowed:
        logger.warning(
            f"[Orchestrator Guardrail Block] Synthesized response blocked by '{output_check.guardrail_name}': {output_check.blocked_reason}"
        )
        response_text = (
            GUARDRAIL_UNAVAILABLE_MESSAGE if output_check.guardrail_name in GUARDRAIL_ERROR_NAMES else
            "I apologize, but the generated response was blocked by our security and safety filters. "
            "Please try rephrasing your request."
        )
    elif output_check.response_text:
        response_text = output_check.response_text
    
    llm_title = None
    if response_text.startswith("TITLE:"):
        parts = response_text.split("\n\n", 1)
        if len(parts) > 1:
            llm_title = parts[0].replace("TITLE:", "").strip()
            response_text = parts[1].strip()
    project_row = None
    try:
        project_row = store.get_project(conversation_id)
    except Exception as e:
        logger.debug(f"[Supabase] Project check skipped: {e}")
    current_title = conv.get("title") or DEFAULT_TITLE
    first_user_message = next((m.get("text") or "" for m in content_array if m.get("role") == "user"), user_query)
    # The project name becomes the title once the user approved it and the Azure DevOps project exists
    official_name = (project_row or {}).get("project_name") if (project_row or {}).get("ado_project_id") else None
    new_title = resolve_conversation_title(current_title, llm_title, first_user_message, official_name)
    if new_title != current_title:
        try:
            get_supabase_client().table("Conversation").update({"title": new_title}).eq("conv_id", conversation_id).execute()
            logger.info(f"[Supabase] Conversation title: '{current_title}' -> '{new_title}'")
            current_title = new_title
        except Exception as e:
            logger.error(f"[Supabase] Failed to update title: {str(e)}")

    # Persist updated conversation history
    content_array.append({"role": "user", "text": user_query})
    content_array.append({"role": "assistant", "text": response_text})
    supabase = get_supabase_client()
    try:
        supabase.table("Conversation").update({"content": content_array}).eq("conv_id", conversation_id).execute()
        logger.info(f"[Supabase] Saved updated conversation history ({len(content_array)} messages total).")
    except Exception as e:
        logger.error(f"[Supabase] Failed to save conversation history: {str(e)}")
        raise

    total_time = round((time.time() - overall_start) * 1000, 1)
    logger.info(f"=== Chat pipeline completed successfully in {total_time}ms ===")

    return ChatResponse(conversation_id=conversation_id, response=response_text,
                        suggestions=get_suggestions(conversation_id),
                        title=current_title)


