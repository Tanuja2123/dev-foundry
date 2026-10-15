"""
Orchestrator routing: the structured decision the Orchestrator returns (Microsoft Agent Framework
`response_format`), the workflow stages, and which operations are allowed in each stage.

The model decides *what the user wants*; this module decides *whether that step is allowed now*,
so the workflow can never skip a stage (e.g. jump from the TRD to the ADO project proposal).
"""
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from app.services import project_store as store

REQUIREMENTS_OPERATIONS = ("GENERATE_PRD", "UPDATE_PRD", "GENERATE_TRD", "UPDATE_TRD")
REPO_SETUP_OPERATIONS = (
    "GENERATE_PROJECT_PROPOSAL", "UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT", "UPDATE_ADO_PROJECT",
    "GENERATE_REPOSITORY_PROPOSAL", "UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY",
    "GENERATE_REPOSITORY_STRUCTURE", "UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE",
)
# Azure Boards work items from the PRD: a parallel track, available once the Azure DevOps project exists
BACKLOG_OPERATIONS = ("GENERATE_WORK_ITEMS_PROPOSAL", "UPDATE_WORK_ITEMS_PROPOSAL", "CREATE_WORK_ITEMS")
OPERATION_AGENT = {op: "Requirements_Agent" for op in REQUIREMENTS_OPERATIONS}
OPERATION_AGENT.update({op: "Repo_Setup_Agent" for op in REPO_SETUP_OPERATIONS})
OPERATION_AGENT.update({op: "Backlog_Agent" for op in BACKLOG_OPERATIONS})

Operation = Literal[
    "GENERATE_PRD", "UPDATE_PRD", "GENERATE_TRD", "UPDATE_TRD",
    "GENERATE_PROJECT_PROPOSAL", "UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT", "UPDATE_ADO_PROJECT",
    "GENERATE_REPOSITORY_PROPOSAL", "UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY",
    "GENERATE_REPOSITORY_STRUCTURE", "UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE",
    "GENERATE_WORK_ITEMS_PROPOSAL", "UPDATE_WORK_ITEMS_PROPOSAL", "CREATE_WORK_ITEMS",
]


class RoutingDecision(BaseModel):
    """Structured output of the Orchestrator for one user message."""
    reasoning: str = Field(description="One short sentence explaining the decision (logged, never shown to the user).")
    user_intent: Literal["approve_only", "explicit_next_step", "change_request", "question", "other"] = Field(
        "other",
        description=(
            "What the latest user message does. approve_only: approves/praises the current document or proposal without "
            "explicitly asking for the next step ('PRD looks good', 'approved', 'the TRD looks good'). explicit_next_step: "
            "explicitly asks for the next step or to create/push something ('create the TRD', 'proceed with repository setup', "
            "'create the project', 'push it'), or answers 'yes' to the assistant's direct question offering that step. "
            "change_request: an instruction to add, change, remove, replace or use something (e.g. 'Use PostgreSQL as the "
            "database', 'add SMS reminders') - EVEN IF it matches what the document already contains. "
            "question: asks a question."))
    mentions_technology: bool = Field(
        False, description="True if the user names a language, framework, database, library, cloud or infrastructure service.")
    action: Literal["delegate", "respond"] = Field(description="'delegate' to run one child-agent operation, 'respond' to answer directly.")
    agent: Optional[Literal["Requirements_Agent", "Repo_Setup_Agent", "Backlog_Agent"]] = Field(
        None, description="Child agent, required when action is 'delegate'.")
    operation: Optional[Operation] = Field(None, description="Operation, required when action is 'delegate'. Must be one of allowed_operations.")
    reply: Optional[str] = Field(None, description="Markdown answer to the user, required when action is 'respond'.")
    different_product: bool = Field(
        False,
        description=(
            "True only when the state shows this chat already has a PRD AND the latest message describes a DIFFERENT "
            "product (another app or system, not a change to the current one), without explicitly asking to replace "
            "the current PRD. False for changes to the current product and for 'replace the PRD with this'."))
    show_document: Optional[Literal["PRD", "TRD"]] = Field(
        None,
        description=(
            "Set (with action 'respond') when the user asks to see, show, open or find the current PRD or TRD "
            "('where is the PRD?', 'show me the TRD'). The stored document is then shown by the system; never write it "
            "yourself in reply."))
    title: Optional[str] = Field(
        None,
        description=(
            "2-5 word name of the PRODUCT the user wants to build, based on the whole conversation (e.g. 'School "
            "Management System', 'Clinic Appointment Booking'). Never the user's own words, never a question, never "
            "a placeholder. Null if no specific product has been described yet (greetings, small talk, questions "
            "about DevFoundry such as 'what can you do?', 'what are you up to?')."
        ),
    )
    title_confidence: float = Field(
        0.0,
        description=(
            "0.0-1.0: how clearly the conversation so far describes ONE specific software product. 0.0 = greetings, "
            "small talk or questions about DevFoundry; 0.3 = a vague wish ('I need an app'); 0.6 = a category without "
            "purpose ('a management system'); 0.8 = a concrete product ('an appointment booking web app for a "
            "clinic'); 1.0 = a detailed description. The title is only used from 0.7."
        ),
    )


# Stage -> operations allowed in that stage (in workflow order)
STAGE_ORDER = (
    store.STAGE_REQUIREMENTS, store.STAGE_PRD_REVIEW, store.STAGE_TRD_REVIEW, store.STAGE_PROJECT_REVIEW,
    store.STAGE_REPOSITORY_REVIEW, store.STAGE_STRUCTURE_REVIEW, store.STAGE_COMPLETE,
)
# Stage-specific steps. The PRD and TRD can be updated in every stage once they exist (see allowed_operations).
ALLOWED_OPERATIONS: Dict[str, Tuple[str, ...]] = {
    store.STAGE_REQUIREMENTS: ("GENERATE_PRD", "UPDATE_PRD"),
    store.STAGE_PRD_REVIEW: ("GENERATE_PRD", "GENERATE_TRD"),
    store.STAGE_TRD_REVIEW: ("GENERATE_TRD", "GENERATE_PROJECT_PROPOSAL"),
    store.STAGE_PROJECT_REVIEW: ("UPDATE_PROJECT_PROPOSAL", "GENERATE_PROJECT_PROPOSAL", "CREATE_PROJECT"),
    store.STAGE_REPOSITORY_REVIEW: ("UPDATE_ADO_PROJECT", "UPDATE_REPOSITORY_PROPOSAL", "GENERATE_REPOSITORY_PROPOSAL",
                                    "CREATE_REPOSITORY"),
    store.STAGE_STRUCTURE_REVIEW: ("UPDATE_ADO_PROJECT", "UPDATE_REPOSITORY_STRUCTURE", "GENERATE_REPOSITORY_STRUCTURE",
                                   "PUSH_REPOSITORY_STRUCTURE"),
    # After the push the structure can still evolve: changes are pushed as a follow-up commit.
    store.STAGE_COMPLETE: ("UPDATE_ADO_PROJECT", "UPDATE_REPOSITORY_STRUCTURE", "GENERATE_REPOSITORY_STRUCTURE",
                           "PUSH_REPOSITORY_STRUCTURE"),
}

STAGE_HINTS = {
    store.STAGE_REQUIREMENTS: "Describe the product you want to build (or upload a requirements document) and I'll draft the Product Requirements Document (PRD).",
    store.STAGE_PRD_REVIEW: "The PRD is ready for your review. You can ask for changes, or ask me to create the Technical Requirements Document (TRD).",
    store.STAGE_TRD_REVIEW: "The TRD is ready for your review. You can ask for technology changes, or ask me to proceed with repository setup.",
    store.STAGE_PROJECT_REVIEW: "The Azure DevOps project proposal is ready. You can change the name or description, or approve it to create the project.",
    store.STAGE_REPOSITORY_REVIEW: "The Azure DevOps project exists. You can change the suggested repository name, or approve it to create the repository.",
    store.STAGE_STRUCTURE_REVIEW: "The repository folder structure is ready for review. You can request changes, or approve it to push it to the main branch.",
    store.STAGE_COMPLETE: "Repository setup is complete. You can still update the PRD, TRD, project details or repository structure; changes are pushed as a new commit.",
}


def _documents(state: Optional[Dict[str, Any]]) -> Tuple[bool, bool]:
    project = ((state or {}).get("project") or {})
    return bool(project.get("prd")), bool(project.get("trd"))


def effective_stage(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """Current workflow stage, or None when no stored state is available (routing is then not gated)."""
    if state is None or state.get("error"):
        return None
    project = state.get("project")
    if not project:
        return store.STAGE_REQUIREMENTS
    stage = project.get("workflow_stage") or store.STAGE_REQUIREMENTS
    if stage == store.STAGE_REQUIREMENTS:  # rows written before PRD_REVIEW/TRD_REVIEW existed
        if project.get("trd"):
            return store.STAGE_TRD_REVIEW
        if project.get("prd"):
            return store.STAGE_PRD_REVIEW
    return stage


def allowed_operations(stage: Optional[str], state: Optional[Dict[str, Any]] = None) -> List[str]:
    if stage is None:
        return list(OPERATION_AGENT)
    has_prd, has_trd = _documents(state)
    if stage == store.STAGE_REQUIREMENTS and not has_prd:
        return ["GENERATE_PRD", "UPDATE_PRD"]
    operations = list(ALLOWED_OPERATIONS.get(stage, ()))
    # If the repository exists in STRUCTURE_REVIEW but structure has not been saved yet,
    # allow CREATE_REPOSITORY to idempotently complete structure creation
    if stage == store.STAGE_STRUCTURE_REVIEW and state:
        repo = state.get("repository") or {}
        if not repo.get("structure") and "CREATE_REPOSITORY" not in operations:
            operations.append("CREATE_REPOSITORY")
    # The PRD and TRD can be updated at any time; downstream artifacts are then flagged as outdated.
    if has_prd and "UPDATE_PRD" not in operations:
        operations.append("UPDATE_PRD")
    if has_trd and "UPDATE_TRD" not in operations:
        operations.append("UPDATE_TRD")
    if has_prd and not has_trd and "GENERATE_TRD" not in operations:
        operations.append("GENERATE_TRD")
    # Azure Boards work items: once the ADO project exists, in any later stage
    project = (state or {}).get("project") or {}
    if has_prd and project.get("ado_project_id"):
        operations.append("GENERATE_WORK_ITEMS_PROPOSAL")
        if project.get("work_items_proposal"):
            operations += ["UPDATE_WORK_ITEMS_PROPOSAL", "CREATE_WORK_ITEMS"]
    return operations


def routing_context(state: Optional[Dict[str, Any]]) -> str:
    """Block appended to the user's latest message for the Orchestrator (never persisted)."""
    stage = effective_stage(state)
    lines = []
    if state is not None:
        lines.append(store.describe_workflow_state(state))
    lines.append(f"current_stage: {stage or 'UNKNOWN'}")
    lines.append(f"allowed_operations: {', '.join(allowed_operations(stage, state)) or 'none (answer directly)'}")
    return "\n".join(lines)


def validate_decision(decision: RoutingDecision, stage: Optional[str],
                      state: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Returns None if the decision may run, otherwise a user-facing explanation of why it is not available now."""
    if decision.action != "delegate":
        return None
    if not decision.operation:
        return "I wasn't sure which step you want to take next."
    if OPERATION_AGENT.get(decision.operation) != decision.agent:
        decision.agent = OPERATION_AGENT.get(decision.operation)  # the operation determines the agent
    if decision.operation not in allowed_operations(stage, state):
        return "That step isn't available at this point of the workflow."
    return None


# Steps that move from one document to the next: they need an explicit request, an approval alone is not enough.
_DOCUMENT_TRANSITIONS = {
    "GENERATE_TRD": "Great, the PRD is approved. Shall I create the Technical Requirements Document (TRD) now?",
    "GENERATE_PROJECT_PROPOSAL": "Great, the TRD is approved. Shall I proceed with repository setup and propose the "
                                 "Azure DevOps project name and description?",
    "CREATE_WORK_ITEMS": "Great, the backlog draft is approved. Shall I create the work items in Azure Boards now?",
}


DIFFERENT_PRODUCT_MARKER = "your message describes a different product"


def different_product_reply(state: Optional[Dict[str, Any]]) -> str:
    project = (state or {}).get("project") or {}
    current = project.get("project_name") if not store.is_placeholder_name(project.get("project_name")) else None
    holder = f"This chat already holds the PRD for **{current}**" if current else "This chat already has a PRD"
    return (f"{holder}, and {DIFFERENT_PRODUCT_MARKER}.\n\n"
            "Each chat is one project, so nothing was changed. To plan the new product, click **New Project** and "
            "paste the description there. If you want to replace this chat's PRD instead, say "
            "\"Replace the PRD with this product\".\n\n---\n\n"
            "Shall I continue with the current project?")


def answers_different_product_question(history: List[Dict[str, Any]]) -> bool:
    """True when the assistant's last reply was the 'different product' question: the user's answer decides now."""
    last = next((m.get("text") or "" for m in reversed(history or []) if m.get("role") == "assistant"), "")
    return DIFFERENT_PRODUCT_MARKER in last


def apply_intent_rules(decision: RoutingDecision, stage: Optional[str],
                       state: Optional[Dict[str, Any]] = None) -> RoutingDecision:
    """
    Deterministic corrections on top of the model's decision:
    - moving to the next document (TRD, repository setup) needs an explicit request, not just an approval;
    - a technology change request is always a TRD update while a TRD exists, even if the model chose to answer directly;
    - a different product described in a chat that already has a PRD never overwrites that PRD: the user is asked first.
    """
    if decision.different_product and _documents(state)[0] and (
            decision.action == "respond" or decision.operation in ("GENERATE_PRD", "UPDATE_PRD")):
        return RoutingDecision(reasoning="different product described in a chat that already has a PRD",
                                user_intent=decision.user_intent, action="respond",
                                reply=different_product_reply(state))
    if (decision.action == "delegate" and decision.operation in _DOCUMENT_TRANSITIONS
            and decision.user_intent in ("approve_only", "question")):
        return RoutingDecision(reasoning=f"{decision.operation} needs an explicit request (intent: {decision.user_intent})",
                                user_intent=decision.user_intent, action="respond",
                                reply=_DOCUMENT_TRANSITIONS[decision.operation])
    if (decision.action == "respond" and decision.user_intent == "change_request" and decision.mentions_technology
            and "UPDATE_TRD" in allowed_operations(stage, state) and _documents(state)[1]):
        return RoutingDecision(reasoning="technology change request -> TRD update", user_intent="change_request",
                                mentions_technology=True, action="delegate", agent="Requirements_Agent",
                                operation="UPDATE_TRD")
    return decision


def parse_routing_decision(response: Any) -> RoutingDecision:
    """Reads the typed decision from an Agent Framework response; falls back to parsing its text as JSON."""
    try:
        value = response.value
        if isinstance(value, RoutingDecision):
            return value
    except Exception:
        pass
    text = response.messages[-1].text if getattr(response, "messages", None) else ""
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return RoutingDecision.model_validate_json(text[start:end + 1])
        except Exception:
            pass
    return RoutingDecision(reasoning="unstructured orchestrator output", action="respond",
                           reply=text.strip() or "Could you rephrase that?")


def blocked_reply(stage: Optional[str], reason: str) -> str:
    hint = STAGE_HINTS.get(stage or "", "")
    return f"{reason} {hint}".strip()


# ---------------------------------------------------------------------------
# Next-step suggestions (shown as chips above the chat input)
# ---------------------------------------------------------------------------

_OUTDATED_SUGGESTIONS = {
    store.TRD_OUTDATED: ("Update the TRD", "Update the TRD to reflect the latest PRD changes."),
    store.PROJECT_PROPOSAL_OUTDATED: ("Refresh project proposal",
                                      "Update the project name and description to reflect the latest requirements."),
    store.ADO_PROJECT_OUTDATED: ("Update Azure DevOps project",
                                 "Update the Azure DevOps project description (and name, if needed) to reflect the latest requirements."),
    store.STRUCTURE_OUTDATED: ("Update repository structure", "Update the repository structure to reflect the latest TRD."),
    store.WORK_ITEMS_OUTDATED: ("Update work items", "Update the Azure Boards work items draft to reflect the latest PRD."),
}


def next_steps(state: Optional[Dict[str, Any]], limit: int = 3) -> List[Dict[str, str]]:
    """Deterministic next-step suggestions from the stored state: outdated artifacts first, then the next workflow step."""
    stage = effective_stage(state)
    if stage is None:
        return []
    outdated = store.outdated_items(state)
    suggestions = [_OUTDATED_SUGGESTIONS[item] for item in outdated if item in _OUTDATED_SUGGESTIONS]
    repository = (state or {}).get("repository") or {}

    # Only suggest moving forward when nothing upstream is outdated
    if not suggestions:
        if stage == store.STAGE_PRD_REVIEW:
            suggestions.append(("Create the TRD", "The PRD looks comprehensive and approved. Please generate the detailed Technical Requirements Document (TRD) covering technology stack, system architecture, and API design."))
        elif stage == store.STAGE_TRD_REVIEW:
            suggestions.append(("Proceed to repository setup", "The TRD architecture and technology stack are approved. Let's proceed with repository setup and propose the Azure DevOps project name and description."))
        elif stage == store.STAGE_PROJECT_REVIEW:
            suggestions.append(("Create the project", "The proposed Azure DevOps project name and description look good. Please go ahead and create the Azure DevOps project."))
        elif stage == store.STAGE_REPOSITORY_REVIEW:
            if repository.get("repo_name"):
                suggestions.append(("Create the repository", "The proposed repository name is approved. Please create the Git repository in Azure DevOps."))
            else:
                suggestions.append(("Suggest a repository name", "Please analyze the project requirements and recommend suitable repository names."))
    if store.STRUCTURE_CHANGES_NOT_PUSHED in outdated and store.STRUCTURE_OUTDATED not in outdated:
        suggestions.append(("Push the changes", "The folder structure changes look good. Please push these structure updates as a new commit to Azure DevOps."))
    elif stage == store.STAGE_STRUCTURE_REVIEW and repository.get("pushed_structure") is None and not suggestions:
        if not repository.get("structure"):
            suggestions.append(("Propose folder structure", "Please propose the complete skeleton folder structure for the repository based on the approved TRD architecture."))
        else:
            suggestions.append(("Push to main", "The proposed repository folder structure is approved. Please push the initial structure to the main branch in Azure DevOps."))
    # Azure Boards work items: offered once the repository is pushed (or as soon as a draft is waiting for approval)
    project = (state or {}).get("project") or {}
    if (project.get("ado_project_id") and store.has_work_item_columns(project)
            and store.WORK_ITEMS_OUTDATED not in outdated):
        draft, created = project.get("work_items_proposal"), project.get("work_items")
        if draft and _draft_not_created(project):
            suggestions.append(("Create work items", "The work items look good. Please create them in Azure Boards."))
        elif not draft and not created and stage == store.STAGE_COMPLETE:
            suggestions.append(("Plan work items", "Draft the Azure Boards work items (epic, features and one story "
                                                   "per requirement with acceptance criteria) from the PRD."))
    return [{"label": label, "prompt": prompt} for label, prompt in suggestions[:limit]]


def _draft_not_created(project: Dict[str, Any]) -> bool:
    """True when the draft (or a change to it) is not in Azure Boards yet: the draft is marked "synced" on creation."""
    return not (project.get("work_items_proposal") or {}).get("synced")
