"""
Supabase persistence for the "Project" and "Repository" tables (see backend/db/migrations/001_*.sql).

Project:    one row per conversation (PRD/TRD, ADO project proposal, created ADO project, workflow stage).
Repository: repositories of a project, including the reviewed folder structure that is pushed to Azure DevOps.
"""
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.logger import logger
from app.services.repo_structure import layer_changes

PROJECT_TABLE = "Project"
REPOSITORY_TABLE = "Repository"

# Project.workflow_stage values
STAGE_REQUIREMENTS = "REQUIREMENTS"
STAGE_PRD_REVIEW = "PRD_REVIEW"
STAGE_TRD_REVIEW = "TRD_REVIEW"
STAGE_PROJECT_REVIEW = "PROJECT_REVIEW"
STAGE_REPOSITORY_REVIEW = "REPOSITORY_REVIEW"
STAGE_STRUCTURE_REVIEW = "STRUCTURE_REVIEW"
STAGE_COMPLETE = "COMPLETE"

# Repository.status values
REPO_PROPOSED = "PROPOSED"
REPO_CREATED = "CREATED"
REPO_STRUCTURE_REVIEW = "STRUCTURE_REVIEW"
REPO_PUSHED = "PUSHED"

MIGRATION_HINT = ("The 'Project'/'Repository' tables are missing. Run "
                  "backend/db/migrations/001_create_project_and_repository.sql in the Supabase SQL editor.")


class StoreError(Exception):
    pass


# Values a model writes when it has no real name ("placeholder", "<project name>", "TBD", "New Project"...).
# They must never become a project / repository name or a conversation title.
_PLACEHOLDER_NAME = re.compile(
    r"^[\s<\[{(\"'`]*(placeholder|project[ _-]?name|repo(sitory)?[ _-]?name|project[ _-]?description|description|"
    r"name|title|tbd|tbc|todo|n/?a|none|null|undefined|untitled( project)?|new project|new conversation|my project|"
    r"example( project)?|sample( project)?|string|your project( name)?|xxx+|\.{2,}|-+|_+|\?+)[\s>\]})\"'`.]*$",
    re.IGNORECASE)


def is_placeholder_name(value: Any) -> bool:
    """True for empty values and generic placeholders that a model might invent instead of a real name."""
    text = str(value or "").strip()
    return not text or bool(_PLACEHOLDER_NAME.match(text))


def _default_client():
    from app.services.conversation_service import get_supabase_client
    return get_supabase_client()


# Tests replace this with a fake client factory.
client_factory: Callable[[], Any] = _default_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute(query, action: str) -> List[Dict[str, Any]]:
    try:
        return query.execute().data or []
    except Exception as e:
        message = str(e)
        if "PGRST205" in message or "schema cache" in message:
            logger.error(f"[ProjectStore] {MIGRATION_HINT}")
            raise StoreError(MIGRATION_HINT) from e
        logger.error(f"[ProjectStore] Failed to {action}: {message}")
        raise StoreError(f"Failed to {action}: {message}") from e


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

def get_project(conv_id: str) -> Optional[Dict[str, Any]]:
    rows = _execute(client_factory().table(PROJECT_TABLE).select("*").eq("conv_id", conv_id).limit(1),
                    "read project")
    return rows[0] if rows else None


def ensure_project(conv_id: str) -> Dict[str, Any]:
    """The conversation's Project row (created on first use). The owner is the conversation's user_id."""
    project = get_project(conv_id)
    if project:
        return project
    rows = _execute(client_factory().table(PROJECT_TABLE).insert({
        "conv_id": conv_id, "workflow_stage": STAGE_REQUIREMENTS,
    }), "create project")
    logger.info(f"[ProjectStore] Created Project row for conversation '{conv_id}'")
    return rows[0]


def update_project(project_id: str, **fields: Any) -> Dict[str, Any]:
    fields["updated_at"] = _now()
    rows = _execute(client_factory().table(PROJECT_TABLE).update(fields).eq("project_id", project_id),
                    "update project")
    if not rows:
        raise StoreError(f"Project '{project_id}' not found.")
    return rows[0]


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

def get_active_repository(project_id: str) -> Optional[Dict[str, Any]]:
    """The most recently created repository of the project (the one the workflow is working on)."""
    rows = _execute(client_factory().table(REPOSITORY_TABLE).select("*").eq("project_id", project_id)
                    .order("created_at", desc=True).limit(1), "read repository")
    return rows[0] if rows else None


def create_repository(project_id: str, repo_name: str) -> Dict[str, Any]:
    rows = _execute(client_factory().table(REPOSITORY_TABLE).insert({
        "project_id": project_id, "repo_name": repo_name, "status": REPO_PROPOSED,
    }), "create repository")
    return rows[0]


def update_repository(repo_id: str, **fields: Any) -> Dict[str, Any]:
    fields["updated_at"] = _now()
    rows = _execute(client_factory().table(REPOSITORY_TABLE).update(fields).eq("repo_id", repo_id),
                    "update repository")
    if not rows:
        raise StoreError(f"Repository '{repo_id}' not found.")
    return rows[0]


def propose_repository_name(project_id: str, repo_name: str) -> Dict[str, Any]:
    """Stores the proposed repository name: renames a not-yet-created proposal, otherwise starts a new one."""
    active = get_active_repository(project_id)
    if active and active.get("status") == REPO_PROPOSED:
        if active.get("repo_name") == repo_name:
            return active
        return update_repository(active["repo_id"], repo_name=repo_name)
    return create_repository(project_id, repo_name)


_UNSET: Any = object()


def save_structure(repo_id: str, structure: List[Dict[str, str]], current_version: int,
                   trd_version: Optional[int] = None, scaffold_commands: Any = _UNSET,
                   scaffold_structure: Any = _UNSET) -> Dict[str, Any]:
    """
    Stores a new structure version. `scaffold_commands` / `scaffold_structure` (migration 005) record the generator
    commands and their exact output: pass them when the structure was (re)generated by commands, pass None to clear
    them (hand-written structure), leave them out to keep them (an update on top of the generator output).
    """
    fields: Dict[str, Any] = {"structure": structure, "structure_version": (current_version or 0) + 1,
                              "status": REPO_STRUCTURE_REVIEW}
    if trd_version is not None:
        fields["structure_trd_version"] = trd_version
    if scaffold_commands is not _UNSET:
        fields["scaffold_commands"] = scaffold_commands
    if scaffold_structure is not _UNSET:
        fields["scaffold_structure"] = scaffold_structure
    return update_repository(repo_id, **fields)


def has_scaffold_columns(repository: Optional[Dict[str, Any]]) -> bool:
    """True once migration 005 (scaffold_commands / scaffold_structure) was run."""
    return bool(repository) and "scaffold_structure" in repository


MAX_LISTED_CONTENT_BYTES = 4_000        # contents of hand-added files shown to the agent (generator output is not)
MAX_LISTED_CONTENT_TOTAL = 30_000


def describe_structure(repository: Dict[str, Any]) -> List[str]:
    """
    Compact, LLM-readable view of the saved structure: the generator commands, every path with its size and origin
    ([generated] / [added] / [edited]), and the content of small hand-added or edited files. Generator output is not
    repeated (it can include lock files of hundreds of KB); the agent never needs it to add or change files.
    """
    import json

    files = repository.get("structure") or []
    scaffold = repository.get("scaffold_structure")
    commands = repository.get("scaffold_commands") or []
    lines = []
    if commands:
        lines.append("scaffold_commands (generator commands that produced the base structure, run in this order): "
                     + json.dumps(commands, ensure_ascii=False))
    else:
        lines.append("scaffold_commands: none (the saved structure was not generated by commands)")
    changes = layer_changes(scaffold, files) if scaffold else {"add": [], "edit": [], "delete": []}
    added, edited = set(changes["add"]), set(changes["edit"])
    lines.append(f"current_structure ({len(files)} files; [generated] = unchanged generator output, "
                 "[added] / [edited] = changes on top of it):")
    contents: Dict[str, str] = {}
    budget = MAX_LISTED_CONTENT_TOTAL
    for item in files:
        path = item["path"]
        origin = "added" if path in added else "edited" if path in edited else ("generated" if scaffold else "file")
        size = len(item["content"]) if item.get("encoding") == "base64" else len(item["content"].encode("utf-8"))
        kind = ", binary" if item.get("encoding") == "base64" else ""
        lines.append(f"- {path} ({size} bytes{kind}) [{origin}]")
        if origin in ("added", "edited", "file") and not kind and size <= MAX_LISTED_CONTENT_BYTES and size <= budget:
            contents[path] = item["content"]
            budget -= size
    if changes["delete"]:
        lines.append("removed_generated_files: " + ", ".join(changes["delete"]))
    if contents:
        lines.append("hand_written_file_contents_json: " + json.dumps(contents, ensure_ascii=False))
    return lines


# ---------------------------------------------------------------------------
# Outdated artifacts (documents and artifacts record the version of the document they were built from)
# ---------------------------------------------------------------------------

TRD_OUTDATED = "TRD_OUTDATED"                                # PRD changed after the TRD was written
PROJECT_PROPOSAL_OUTDATED = "PROJECT_PROPOSAL_OUTDATED"      # TRD changed after the (not yet created) proposal
ADO_PROJECT_OUTDATED = "ADO_PROJECT_OUTDATED"                # TRD changed after the ADO project was created/updated
STRUCTURE_OUTDATED = "STRUCTURE_OUTDATED"                    # TRD changed after the repository structure was written
STRUCTURE_CHANGES_NOT_PUSHED = "STRUCTURE_CHANGES_NOT_PUSHED"  # structure edited after it was pushed
WORK_ITEMS_OUTDATED = "WORK_ITEMS_OUTDATED"                  # PRD changed after the work items (or their draft)

OUTDATED_DESCRIPTIONS = {
    WORK_ITEMS_OUTDATED: "the Azure Boards work items (or their draft) do not reflect the latest PRD",
    TRD_OUTDATED: "the TRD does not reflect the latest PRD changes",
    PROJECT_PROPOSAL_OUTDATED: "the project name/description proposal does not reflect the latest TRD",
    ADO_PROJECT_OUTDATED: "the Azure DevOps project name/description does not reflect the latest requirements",
    STRUCTURE_OUTDATED: "the repository structure does not reflect the latest TRD",
    STRUCTURE_CHANGES_NOT_PUSHED: "the repository structure has changes that are not pushed yet",
}


def outdated_items(state: Dict[str, Any]) -> List[str]:
    project = (state or {}).get("project") or {}
    repository = (state or {}).get("repository") or {}
    prd_version = project.get("prd_version") or 0
    trd_version = project.get("trd_version") or 0
    items = []
    if project.get("trd") and (project.get("trd_prd_version") or 0) < prd_version:
        items.append(TRD_OUTDATED)
    if project.get("project_name") and (project.get("proposal_trd_version") or 0) < trd_version:
        items.append(ADO_PROJECT_OUTDATED if project.get("ado_project_id") else PROJECT_PROPOSAL_OUTDATED)
    if repository.get("structure") and (repository.get("structure_trd_version") or 0) < trd_version:
        items.append(STRUCTURE_OUTDATED)
    if repository.get("pushed_structure") is not None and repository.get("structure") != repository.get("pushed_structure"):
        items.append(STRUCTURE_CHANGES_NOT_PUSHED)
    # Outdated = the draft is older than the PRD (redraft it). A current draft whose items are not created yet is not
    # "outdated": its next step is "Create work items" (seen live: the chip kept saying "Update work items").
    draft = project.get("work_items_proposal")
    if ((draft and (project.get("work_items_proposal_prd_version") or 0) < prd_version)
            or (not draft and project.get("work_items") and (project.get("work_items_prd_version") or 0) < prd_version)):
        items.append(WORK_ITEMS_OUTDATED)
    return items


def has_work_item_columns(project: Optional[Dict[str, Any]]) -> bool:
    """True once migration 006 (work_items / work_items_proposal) was run."""
    return bool(project) and "work_items_proposal" in project


def work_items_status(project: Optional[Dict[str, Any]]) -> str:
    """One line for the agents: draft / created work items."""
    project = project or {}
    created = project.get("work_items") or {}
    draft = project.get("work_items_proposal") or {}
    features = draft.get("features") or []
    parts = []
    if draft:
        parts.append(f"draft with {len(features)} features and {sum(len(f.get('stories') or []) for f in features)} "
                     f"stories (from PRD v{project.get('work_items_proposal_prd_version') or 0})")
    if created:
        parts.append(f"{len(created)} work items created in Azure Boards (reflect PRD v"
                     f"{project.get('work_items_prd_version') or 0})")
    return "; ".join(parts) or "none drafted yet"


def has_pr_review_column(repository: Optional[Dict[str, Any]]) -> bool:
    """True once migration 007 (Repository.pr_reviews) was run."""
    return bool(repository) and "pr_reviews" in repository


def save_pr_review(repository: Dict[str, Any], pr_id: int, review: Dict[str, Any]) -> Dict[str, Any]:
    """Stores the latest review of one pull request (Repository.pr_reviews, keyed by PR id)."""
    reviews = dict(repository.get("pr_reviews") or {})
    reviews[str(pr_id)] = review
    return update_repository(repository["repo_id"], pr_reviews=reviews)


def unposted_findings(review: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    posted = (review or {}).get("posted") or {}
    return [f for f in (review or {}).get("findings") or [] if f.get("fingerprint") not in posted]


def pr_reviews_status(repository: Optional[Dict[str, Any]]) -> str:
    """One line for the Orchestrator: the reviewed pull requests and whether their findings were posted."""
    reviews = (repository or {}).get("pr_reviews") or {}
    parts = []
    for pr_id, review in sorted(reviews.items(), key=lambda item: int(item[0]))[-5:]:
        findings, waiting = len(review.get("findings") or []), len(unposted_findings(review))
        state = (f"{waiting} not posted yet" if waiting else "all posted") if findings else "nothing to post"
        parts.append(f"PR #{pr_id} (iteration {review.get('iteration')}): {findings} findings, {state}")
    return "; ".join(parts) or "none reviewed yet"


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------

def get_workflow_state(conv_id: str) -> Dict[str, Any]:
    """Returns {"project": row | None, "repository": row | None}. Never raises; returns an 'error' key instead."""
    try:
        project = get_project(conv_id)
        repository = get_active_repository(project["project_id"]) if project else None
        return {"project": project, "repository": repository}
    except StoreError as e:
        return {"project": None, "repository": None, "error": str(e)}


def describe_workflow_state(state: Dict[str, Any], include_requirements: bool = False,
                            include_structure: bool = False,
                            include_prd: Optional[bool] = None,
                            include_trd: Optional[bool] = None) -> str:
    """Compact, LLM-readable summary of the stored workflow state (injected into agent input, never persisted)."""
    import json

    if state.get("error"):
        return f"[Current Workflow State (database)]\nunavailable: {state['error']}"
    project = state.get("project")
    if not project:
        return "[Current Workflow State (database)]\nNo project record yet (requirements stage)."

    repository = state.get("repository")
    lines = [
        "[Current Workflow State (database)]",
        f"workflow_stage: {project.get('workflow_stage')}",
        f"prd_saved: {bool(project.get('prd'))}, trd_saved: {bool(project.get('trd'))}",
        f"project_name: {project.get('project_name')}",
        f"project_description: {project.get('project_description')}",
        f"ado_project_created: {bool(project.get('ado_project_id'))}"
        + (f" (id={project.get('ado_project_id')}, url={project.get('ado_project_url')})" if project.get("ado_project_id") else ""),
    ]
    if repository:
        files = repository.get("structure") or []
        lines += [
            f"repository_name: {repository.get('repo_name')}",
            f"repository_status: {repository.get('status')}",
            f"repository_created_in_ado: {bool(repository.get('ado_repo_id'))}"
            + (f" (url={repository.get('ado_repo_url')})" if repository.get("ado_repo_id") else ""),
            f"structure_version: {repository.get('structure_version')} ({len(files)} files)",
        ]
        if repository.get("commit_id"):
            lines.append(f"pushed_commit: {repository.get('commit_id')} on {repository.get('branch')}")
        if repository.get("ado_repo_id") and has_pr_review_column(repository):
            lines.append(f"pull_request_security_reviews: {pr_reviews_status(repository)}")
    else:
        lines.append("repository: none proposed yet")
    if project.get("ado_project_id"):
        lines.append(f"azure_boards_work_items: {work_items_status(project)}")
    lines.append(f"document_versions: prd={project.get('prd_version') or 0}, trd={project.get('trd_version') or 0} "
                 f"(trd based on prd {project.get('trd_prd_version') or 0})")
    outdated = outdated_items(state)
    lines.append("outdated: " + ("; ".join(f"{item} ({OUTDATED_DESCRIPTIONS[item]})" for item in outdated) or "none"))

    should_include_prd = include_requirements if include_prd is None else include_prd
    should_include_trd = include_requirements if include_trd is None else include_trd

    if should_include_prd and project.get("prd"):
        lines.append("approved_prd_json: " + json.dumps(project["prd"], ensure_ascii=False))
    if should_include_trd and project.get("trd"):
        lines.append("approved_trd_json: " + json.dumps(project["trd"], ensure_ascii=False))
    if include_structure and repository and repository.get("structure"):
        lines += describe_structure(repository)
    return "\n".join(lines)
