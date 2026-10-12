"""
Supabase persistence for the "Project" and "Repository" tables (see backend/db/migrations/001_*.sql).

Project:    one row per conversation (PRD/TRD, ADO project proposal, created ADO project, workflow stage).
Repository: repositories of a project, including the reviewed folder structure that is pushed to Azure DevOps.
"""
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.logger import logger

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


def save_structure(repo_id: str, structure: List[Dict[str, str]], current_version: int,
                   trd_version: Optional[int] = None) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"structure": structure, "structure_version": (current_version or 0) + 1,
                              "status": REPO_STRUCTURE_REVIEW}
    if trd_version is not None:
        fields["structure_trd_version"] = trd_version
    return update_repository(repo_id, **fields)


# ---------------------------------------------------------------------------
# Outdated artifacts (documents and artifacts record the version of the document they were built from)
# ---------------------------------------------------------------------------

TRD_OUTDATED = "TRD_OUTDATED"                                # PRD changed after the TRD was written
PROJECT_PROPOSAL_OUTDATED = "PROJECT_PROPOSAL_OUTDATED"      # TRD changed after the (not yet created) proposal
ADO_PROJECT_OUTDATED = "ADO_PROJECT_OUTDATED"                # TRD changed after the ADO project was created/updated
STRUCTURE_OUTDATED = "STRUCTURE_OUTDATED"                    # TRD changed after the repository structure was written
STRUCTURE_CHANGES_NOT_PUSHED = "STRUCTURE_CHANGES_NOT_PUSHED"  # structure edited after it was pushed

OUTDATED_DESCRIPTIONS = {
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
    return items


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
    else:
        lines.append("repository: none proposed yet")
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
        lines.append("current_structure_files_json: " + json.dumps(repository["structure"], ensure_ascii=False))
    return "\n".join(lines)
