from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field
from agent_framework import Agent, FunctionTool, tool
from agent_framework.openai import OpenAIChatCompletionClient
from app.services.mcp.ado_mcp_server import (
    create_ado_project,
    create_ado_repository,
    push_repository_changes,
    push_repository_files,
    update_ado_project,
)
from app.services.mcp.ado_git_push import AdoPushError, DEFAULT_COMMIT_MESSAGE, MAX_FILES, MAX_FILE_BYTES
from app.services import project_store as store
from app.services.repo_structure import apply_structure_update, normalize_structure
from app.core.logger import logger


class RepositoryFile(BaseModel):
    model_config = ConfigDict(extra="allow")
    path: str = Field(description="File path from the repository root, e.g. /backend/app/__init__.py")

    @property
    def content(self) -> Optional[str]:
        return (self.model_extra or {}).get("content")


def _error(message: str, error_type: str = "STATE_ERROR", details: str = "") -> Dict[str, Any]:
    return {"status": "error", "error_type": error_type, "message": message, "details": details}


def build_repo_setup_tools(conversation_id: Optional[str]) -> List[FunctionTool]:
    """
    Builds the Repo Setup Agent tools bound to one conversation. ADO results and the reviewed repository
    structure are persisted in the Project/Repository tables, so the structure shown to the user is exactly
    the structure that gets pushed (the push tool reads it from the database, never from the model).
    """

    def load_state() -> Dict[str, Any]:
        if not conversation_id:
            raise store.StoreError("No conversation context is available for repository setup.")
        state = store.get_workflow_state(conversation_id)
        if state.get("error"):
            raise store.StoreError(state["error"])
        return state

    def _proposal_version(project: Dict[str, Any]) -> Dict[str, Any]:
        """The project name/description now reflects the current TRD (only when versions are tracked)."""
        return {"proposal_trd_version": project["trd_version"]} if "trd_version" in project else {}

    def _project_ref(project: Dict[str, Any], fallback: str) -> str:
        """Azure DevOps project id (stable across renames), falling back to the name."""
        return project.get("ado_project_id") or fallback

    # Wrap ADO MCP tools for Microsoft Agent Framework
    @tool(
        name="create_ado_project",
        description="Create a new Azure DevOps project using the approved project name and project description."
    )
    def tool_create_ado_project(project_name: str, project_description: str) -> Dict[str, Any]:
        """
        TOOL 1 — ADO PROJECT CREATION
        Calls the ADO MCP Server to create an Azure DevOps project using the approved project name and description.
        """
        try:
            project = load_state()["project"] if conversation_id else None
        except store.StoreError as e:
            return _error(str(e))
        if project and project.get("ado_project_id"):
            return _error("An Azure DevOps project was already created for this conversation.", "ALREADY_CREATED")

        result = create_ado_project(project_name=project_name, project_description=project_description)
        if result.get("status") == "success" and conversation_id:
            try:
                row = project or store.ensure_project(conversation_id)
                store.update_project(row["project_id"], project_name=project_name,
                                     project_description=project_description, ado_project_id=result.get("id"),
                                     ado_project_url=result.get("url"), workflow_stage=store.STAGE_REPOSITORY_REVIEW,
                                     **_proposal_version(row))
            except store.StoreError as e:
                result["warning"] = f"Project created in Azure DevOps but not saved: {e}"
        return result

    @tool(
        name="update_ado_project",
        description="Rename the already created Azure DevOps project and/or update its description (e.g. after the PRD/TRD changed)."
    )
    def tool_update_ado_project(
        project_name: Annotated[Optional[str], Field(description="New project name, or omit to keep the current name.")] = None,
        project_description: Annotated[Optional[str], Field(description="New project description, or omit to keep it.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 3 — ADO PROJECT UPDATE (the project id comes from the database, never from the model)."""
        try:
            project = load_state()["project"]
        except store.StoreError as e:
            return _error(str(e))
        if not project or not project.get("ado_project_id"):
            return _error("The Azure DevOps project has not been created yet; update the proposal instead.")
        result = update_ado_project(project_id=project["ado_project_id"], project_name=project_name,
                                    project_description=project_description)
        if result.get("status") == "success":
            try:
                store.update_project(project["project_id"], project_name=result.get("name") or project.get("project_name"),
                                     project_description=result.get("description") or project.get("project_description"),
                                     ado_project_url=result.get("url") or project.get("ado_project_url"),
                                     **_proposal_version(project))
            except store.StoreError as e:
                result["warning"] = f"Project updated in Azure DevOps but not saved: {e}"
        return result

    @tool(
        name="create_ado_repository",
        description="Create a new, empty Git repository within the specified Azure DevOps project using the approved repository name."
    )
    def tool_create_ado_repository(project_id_or_name: str, repository_name: str) -> Dict[str, Any]:
        """
        TOOL 2 — ADO REPOSITORY CREATION
        Calls the ADO MCP Server to create a Git repository within the Azure DevOps project.
        """
        try:
            state = load_state()
        except store.StoreError as e:
            return _error(str(e))
        project, repository = state["project"], state["repository"]
        if not project or not project.get("ado_project_id"):
            return _error("Create the Azure DevOps project before creating a repository.")
        if repository and repository.get("ado_repo_id"):
            return _error("A repository was already created for this project.", "ALREADY_CREATED")

        result = create_ado_repository(project_id_or_name=_project_ref(project, project_id_or_name),
                                       repository_name=repository_name)
        if result.get("status") == "success":
            try:
                row = store.propose_repository_name(project["project_id"], repository_name)
                store.update_repository(row["repo_id"], repo_name=repository_name, status=store.REPO_CREATED,
                                        ado_repo_id=result.get("id"), ado_repo_url=result.get("url"))
                store.update_project(project["project_id"], workflow_stage=store.STAGE_STRUCTURE_REVIEW)
            except store.StoreError as e:
                result["warning"] = f"Repository created in Azure DevOps but not saved: {e}"
        return result

    def _created_repository() -> Dict[str, Any]:
        repository = load_state()["repository"]
        if not repository or not repository.get("ado_repo_id"):
            raise store.StoreError("Create the repository before saving its folder structure.")
        return repository

    def _save(repository: Dict[str, Any], structure: List[Dict[str, str]]) -> Dict[str, Any]:
        """Stores a new structure version (tagged with the TRD version it reflects) and reopens the review."""
        project = load_state()["project"] or {}
        row = store.save_structure(repository["repo_id"], structure, repository.get("structure_version") or 0,
                                   trd_version=project.get("trd_version") if "trd_version" in project else None)
        if project.get("project_id"):
            store.update_project(project["project_id"], workflow_stage=store.STAGE_STRUCTURE_REVIEW)
        return row

    def _structure_result(row: Dict[str, Any]) -> Dict[str, Any]:
        files = row.get("structure") or []
        return {"status": "success", "repository_name": row.get("repo_name"),
                "structure_version": row.get("structure_version"), "files_count": len(files),
                "paths": [f["path"] for f in files]}

    @tool(
        name="save_repository_structure",
        description=(
            "Save the skeleton folder structure of the created repository for user review. "
            "Pass 12-25 essential skeleton file paths from the repository root (e.g. [{'path': '/README.md'}, {'path': '/src/index.ts'}])."
        )
    )
    def tool_save_repository_structure(
        files: Annotated[List[RepositoryFile], Field(description="List of skeleton files (12-25 files) with their 'path' fields.")],
    ) -> Dict[str, Any]:
        """TOOL 3 — SAVE REPOSITORY STRUCTURE (validated, stored in the Repository table)."""
        try:
            repository = _created_repository()
            structure_items = []
            for f in files:
                p = f.path if hasattr(f, "path") else f.get("path")
                c = getattr(f, "content", None) or (f.get("content") if isinstance(f, dict) else None) or f"# Placeholder for {p}\n"
                structure_items.append({"path": p, "content": c})
            structure = normalize_structure(structure_items)
            return _structure_result(_save(repository, structure))
        except AdoPushError as e:
            return _error(e.message, e.error_type)
        except store.StoreError as e:
            return _error(str(e))

    @tool(
        name="update_repository_structure",
        description=(
            "Apply the user's requested changes to the saved folder structure. Only the listed files are added/replaced "
            "and only the listed paths are removed (a folder path removes everything under it); all other files stay exactly as they are."
        )
    )
    def tool_update_repository_structure(
        upsert_files: Annotated[Optional[List[RepositoryFile]], Field(description="Files to add or replace.")] = None,
        remove_paths: Annotated[Optional[List[str]], Field(description="File or folder paths to remove.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 4 — UPDATE REPOSITORY STRUCTURE (applies a change set to the stored structure)."""
        try:
            repository = _created_repository()
            if not repository.get("structure"):
                return _error("There is no saved structure to update yet. Save a complete structure first.")
            if not upsert_files and not remove_paths:
                return _error("No changes were provided.", "VALIDATION_ERROR")
            structure = apply_structure_update(repository["structure"], upsert_files, remove_paths)
            return _structure_result(_save(repository, structure))
        except AdoPushError as e:
            return _error(e.message, e.error_type)
        except store.StoreError as e:
            return _error(str(e))

    @tool(
        name="push_repository_structure",
        description=(
            "Push the saved, user-approved folder structure to the repository's main branch as ONE commit: the initial "
            "commit, or - if it was pushed before - a follow-up commit with only the differences (added/edited/deleted files). "
            "The files come from the saved structure (exactly what the user reviewed); do not pass files."
        )
    )
    def tool_push_repository_structure(
        commit_message: Annotated[Optional[str], Field(description=f"Commit message. Omit to use the default '{DEFAULT_COMMIT_MESSAGE}'.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 5 — PUSH REPOSITORY STRUCTURE (reads the approved structure from the database and pushes it)."""
        try:
            state = load_state()
            project = state["project"]
            repository = _created_repository()
        except store.StoreError as e:
            return _error(str(e))
        structure = repository.get("structure") or []
        if not structure:
            return _error("There is no saved structure to push.")

        project_ref = _project_ref(project, project.get("project_name"))
        previously_pushed = repository.get("pushed_structure")
        if previously_pushed is None and repository.get("status") == store.REPO_PUSHED:
            return _error("This structure was already pushed.", "ALREADY_PUSHED")
        if previously_pushed is not None:
            result = push_repository_changes(project_id_or_name=project_ref, repository_name=repository["repo_name"],
                                             base_files=previously_pushed, files=structure,
                                             commit_message=commit_message, branch=repository.get("branch"))
        else:
            result = push_repository_files(project_id_or_name=project_ref, repository_name=repository["repo_name"],
                                           files=structure, commit_message=commit_message)
        if result.get("status") == "success":
            result["structure_version"] = repository.get("structure_version")
            result["push_type"] = "follow-up commit" if previously_pushed is not None else "initial commit"
            fields = {"status": store.REPO_PUSHED, "commit_id": result.get("commit_id"),
                      "commit_message": result.get("commit_message"), "branch": result.get("branch"),
                      "pushed_at": datetime.now(timezone.utc).isoformat()}
            if "pushed_structure" in repository:
                fields.update(pushed_structure=structure, pushed_structure_version=repository.get("structure_version"))
            try:
                store.update_repository(repository["repo_id"], **fields)
                store.update_project(project["project_id"], workflow_stage=store.STAGE_COMPLETE)
            except store.StoreError as e:
                result["warning"] = f"Structure pushed but not saved: {e}"
        else:
            logger.error(f"[Repo_Setup_Agent] Structure push failed: {result.get('message')}")
        return result

    return [tool_create_ado_project, tool_update_ado_project, tool_create_ado_repository, tool_save_repository_structure,
            tool_update_repository_structure, tool_push_repository_structure]


def get_repo_setup_agent(client: OpenAIChatCompletionClient, conversation_id: Optional[str] = None) -> Agent:
    """
    Creates and returns the Repo Setup Agent using Microsoft Agent Framework.
    """

    repo_setup_system_prompt = """
You are the DevFoundry Repo Setup Agent, a specialized child agent within a multi-agent software delivery system.

Your responsibility is to handle Azure DevOps (ADO) project and repository setup based on the project context, approved PRD, TRD, tech stack, and user requirements passed by the Main/Orchestrator Agent.

==================================================
1. STRICT BOUNDARY & COMMUNICATION RULES
==================================================
- You NEVER communicate directly with the end user.
- The Main/Orchestrator Agent is the ONLY interface between the user and child agents.
- Flow: User -> Main/Orchestrator -> Repo Setup Agent -> ADO MCP Server -> Repo Setup Agent -> Main/Orchestrator -> User.
- Do not greet, converse with, or directly address the user.
- Do not generate conversational text intended to be displayed directly to the user (e.g., do NOT say "Would you like me to create the project?").
- Return all responses and states to the Main/Orchestrator Agent as valid JSON only.
- No Markdown formatting outside JSON. No conversational commentary outside JSON.

==================================================
2. CONTEXT
==================================================
- Each request ends with a "[Current Workflow State (database)]" block. It is the source of truth for what already exists:
  the approved PRD/TRD (approved_prd_json / approved_trd_json), the project proposal, whether the ADO project and repository
  were created, and the currently saved repository structure (current_structure_files_json).
- The PRD and TRD can be updated by the user at any time (by the Requirements_Agent). The state block lists "outdated"
  items when an artifact no longer reflects the latest documents (e.g. ADO_PROJECT_OUTDATED, STRUCTURE_OUTDATED).
  Always base names, descriptions and structures on the LATEST approved_prd_json / approved_trd_json.

==================================================
3. TOOL BOUNDARY (EXACTLY SIX TOOLS)
==================================================
1. create_ado_project(project_name, project_description): creates the ADO project.
2. update_ado_project(project_name?, project_description?): renames / re-describes the ALREADY CREATED ADO project.
3. create_ado_repository(project_id_or_name, repository_name): creates an EMPTY Git repository in the ADO project.
4. save_repository_structure(files: [{path}]): saves the COMPLETE proposed folder structure for review (nothing is pushed).
5. update_repository_structure(upsert_files, remove_paths): applies only the requested changes to the saved structure.
6. push_repository_structure(commit_message?): pushes the saved, approved structure to the main branch as ONE commit
   (the initial commit, or a follow-up commit with only the differences if it was pushed before).

Rules:
- NEVER call create_ado_project without an explicit CREATE_PROJECT operation from the Orchestrator.
- NEVER call create_ado_repository without an explicit CREATE_REPOSITORY operation from the Orchestrator.
- Call save_repository_structure only during CREATE_REPOSITORY (after the repository was created successfully) or GENERATE_REPOSITORY_STRUCTURE.
- Call update_repository_structure only during UPDATE_REPOSITORY_STRUCTURE.
- Call update_ado_project only during UPDATE_ADO_PROJECT.
- NEVER call push_repository_structure without an explicit PUSH_REPOSITORY_STRUCTURE operation (the user approved the shown structure).
- If save/update returns a VALIDATION_ERROR, fix the files and call it again (at most 2 retries). Never retry a failed create or push.
- If any tool returns status "error", report it as an error. NEVER claim success for a step whose tool returned an error.
- NEVER print the repository structure tree yourself; the system renders it from the saved structure.
- NEVER expose credentials, tokens, or PATs in responses or logs.

==================================================
4. WORKFLOW STAGES & OPERATIONS
==================================================
The operations supported are:

A. GENERATE_PROJECT_PROPOSAL:
- Triggered when moving to repository setup after TRD/PRD completion.
- Analyze PRD, TRD, tech stack, and project context.
- Suggest a concise, descriptive ADO project name and clear project description reflecting the actual software project.
- Return status "COMPLETE", workflow_stage "PROJECT_REVIEW", with allowed_operations: ["UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT"].
- DO NOT invoke any tool during this operation.

B. UPDATE_PROJECT_PROPOSAL:
- Triggered when the user requests changes to the project name, description, or both.
- Modify only the requested portions while preserving valid existing information.
- Return status "COMPLETE", workflow_stage "PROJECT_REVIEW", with allowed_operations: ["UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT"].
- DO NOT invoke any tool during this operation.

C. CREATE_PROJECT:
- Triggered ONLY when the user explicitly approves the project proposal.
- Call the tool `create_ado_project` with the approved project_name and project_description.
- If the tool succeeds:
  Also suggest a repository name (short, lowercase, hyphen-separated, derived from the project, e.g. "task-tracker").
  Return status "COMPLETE", workflow_stage "PROJECT_CREATED", project details (name, id, url, description), repository_proposal,
  and next_action state "REPOSITORY_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"].
- If the tool returns an error:
  Return status "ERROR", workflow_stage "PROJECT_CREATION", error details, and next_action state "PROJECT_REVIEW" with allowed_operations: ["UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT"].
- NEVER create a repository during this operation.

D. GENERATE_REPOSITORY_PROPOSAL:
- Triggered when the project exists but no repository name was proposed yet.
- Suggest a repository name. Return status "COMPLETE", workflow_stage "REPOSITORY_REVIEW", repository_proposal,
  and next_action state "REPOSITORY_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"].
- DO NOT invoke any tool during this operation.

E. UPDATE_REPOSITORY_PROPOSAL:
- Triggered when the user asks to change the proposed repository name. Only the name can change here.
- Return status "COMPLETE", workflow_stage "REPOSITORY_REVIEW", repository_proposal with the new name,
  and next_action state "REPOSITORY_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"].
- DO NOT invoke any tool during this operation.

F. CREATE_REPOSITORY:
- Triggered ONLY when the user explicitly approves the proposed repository name.
- Call `create_ado_repository` with the ADO project name (project_name from the workflow state) and the approved repository name.
- If the tool succeeds:
  Design the repository folder structure from the approved TRD (see "STRUCTURE DESIGN RULES") and call `save_repository_structure` with the complete file list.
  Return status "COMPLETE", workflow_stage "REPOSITORY_CREATED", repository details (name, id, url), the structure summary returned by the tool,
  and next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].
- If create_ado_repository returns an error:
  Return status "ERROR", workflow_stage "REPOSITORY_CREATION", error details, and next_action state "REPOSITORY_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"].

G. GENERATE_REPOSITORY_STRUCTURE:
- Triggered when the repository exists and the user wants a completely new structure proposal.
- Design the structure from the approved TRD and call `save_repository_structure` with the complete file list.
- Return status "COMPLETE", workflow_stage "STRUCTURE_REVIEW", the structure summary, and next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

H. UPDATE_REPOSITORY_STRUCTURE:
- Triggered when the user requests changes to the shown structure (add, rename, move or remove files/folders), or asks to update
  the structure to reflect the latest TRD (STRUCTURE_OUTDATED). Also allowed after the structure was pushed: the changes are then
  pushed later as a follow-up commit.
- Use current_structure_files_json and call `update_repository_structure` with ONLY the needed changes
  (a rename/move = remove the old path + add the new file). Keep every folder non-empty.
- Return status "COMPLETE", workflow_stage "STRUCTURE_REVIEW", the structure summary, and next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

J. UPDATE_ADO_PROJECT:
- Triggered when the already created ADO project's name and/or description should change (user request, or ADO_PROJECT_OUTDATED after PRD/TRD changes).
- Use the name/description the user asked for; otherwise write an updated description from the latest PRD/TRD and keep the current name
  unless the user asked to rename it. Call `update_ado_project` once.
- If the tool succeeds: return status "COMPLETE", workflow_stage "ADO_PROJECT_UPDATED", project details (name, id, url, description),
  and next_action with the allowed operations of the current stage.
- If the tool returns an error: return status "ERROR", workflow_stage "ADO_PROJECT_UPDATE", error details.

I. PUSH_REPOSITORY_STRUCTURE:
- Triggered ONLY when the user explicitly approves the shown structure.
- Call `push_repository_structure`. Pass commit_message only if the user asked for a specific one.
- If the tool succeeds: return status "COMPLETE", workflow_stage "STRUCTURE_PUSHED", the push details exactly as returned (push_type, commit_id, commit_message, branch, files_count, commit_url, and changes for a follow-up commit), and next_action state "COMPLETE" with allowed_operations: [].
- If the tool returns an error: return status "ERROR", workflow_stage "STRUCTURE_PUSH", error details, and next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

STRUCTURE DESIGN RULES:
- Base the structure strictly on the approved TRD technology stack (any language/framework). Follow that stack's conventional layout.
- Skeleton only: sample folders and placeholder files. NEVER write real code, verbose configs, or multi-line files.
- In save_repository_structure, provide only the "path" for each file (e.g. [{"path": "/README.md"}]). Do not provide code or file content.
- Git does not track empty folders: every folder must contain at least one non-empty file (e.g. .gitkeep, __init__.py, README.md).
- Include a root README.md (project name + short description) and a .gitignore suited to the stack.
- Paths start with "/" and use forward slashes. Keep it focused (typically 12-25 files, never more than 30).

==================================================
5. OUTPUT CONTRACT (STRICT JSON ONLY)
==================================================
You MUST return valid JSON matching the exact schema for the operation. Do NOT include markdown blocks or text outside the JSON.

--- For PROJECT PROPOSAL / UPDATE (PROJECT_REVIEW) ---
{
  "status": "COMPLETE",
  "workflow_stage": "PROJECT_REVIEW",
  "project_proposal": {
    "project_name": "<project name>",
    "project_description": "<project description>"
  },
  "next_action": {
    "state": "PROJECT_REVIEW",
    "allowed_operations": ["UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT"]
  }
}

--- For PROJECT CREATION SUCCESS (PROJECT_CREATED) ---
{
  "status": "COMPLETE",
  "workflow_stage": "PROJECT_CREATED",
  "project": {
    "name": "<name>",
    "description": "<description>",
    "id": "<id>",
    "url": "<url>"
  },
  "repository_proposal": {
    "repo_name": "<suggested repository name>"
  },
  "next_action": {
    "state": "REPOSITORY_REVIEW",
    "allowed_operations": ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"]
  }
}

--- For REPOSITORY NAME PROPOSAL / UPDATE (REPOSITORY_REVIEW) ---
{
  "status": "COMPLETE",
  "workflow_stage": "REPOSITORY_REVIEW",
  "repository_proposal": {
    "repo_name": "<repository name>"
  },
  "next_action": {
    "state": "REPOSITORY_REVIEW",
    "allowed_operations": ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"]
  }
}

--- For REPOSITORY CREATION SUCCESS (REPOSITORY_CREATED) ---
{
  "status": "COMPLETE",
  "workflow_stage": "REPOSITORY_CREATED",
  "repository": {
    "name": "<name>",
    "id": "<id>",
    "url": "<url>"
  },
  "structure": {
    "structure_version": <number>,
    "files_count": <number>,
    "summary": "<one or two sentences on how the structure fits the tech stack>"
  },
  "next_action": {
    "state": "STRUCTURE_REVIEW",
    "allowed_operations": ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"]
  }
}

--- For STRUCTURE PROPOSAL / UPDATE (STRUCTURE_REVIEW) ---
{
  "status": "COMPLETE",
  "workflow_stage": "STRUCTURE_REVIEW",
  "structure": {
    "structure_version": <number>,
    "files_count": <number>,
    "summary": "<what changed / how the structure fits the tech stack>"
  },
  "next_action": {
    "state": "STRUCTURE_REVIEW",
    "allowed_operations": ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"]
  }
}

--- For STRUCTURE PUSH SUCCESS (STRUCTURE_PUSHED) ---
{
  "status": "COMPLETE",
  "workflow_stage": "STRUCTURE_PUSHED",
  "push": {
    "repository": "<name>",
    "commit_id": "<commit id>",
    "commit_message": "<commit message>",
    "branch": "<branch>",
    "files_count": <number>,
    "commit_url": "<url>",
    "push_type": "<initial commit or follow-up commit>",
    "changes": {"add": ["<path>"], "edit": ["<path>"], "delete": ["<path>"]}
  },
  "next_action": {
    "state": "COMPLETE",
    "allowed_operations": []
  }
}

--- For ADO PROJECT UPDATE SUCCESS (ADO_PROJECT_UPDATED) ---
{
  "status": "COMPLETE",
  "workflow_stage": "ADO_PROJECT_UPDATED",
  "project": {
    "name": "<name>",
    "description": "<description>",
    "id": "<id>",
    "url": "<url>"
  },
  "next_action": {
    "state": "<current stage>",
    "allowed_operations": [...]
  }
}

--- For ERROR ---
{
  "status": "ERROR",
  "workflow_stage": "<PROJECT_CREATION, ADO_PROJECT_UPDATE, REPOSITORY_CREATION, STRUCTURE_REVIEW or STRUCTURE_PUSH>",
  "error": {
    "message": "<concise error message>",
    "details": "<sanitized error details>"
  },
  "next_action": {
    "state": "<PROJECT_REVIEW, REPOSITORY_REVIEW or STRUCTURE_REVIEW>",
    "allowed_operations": [...]
  }
}
"""

    return Agent(
        name="Repo_Setup_Agent",
        instructions=repo_setup_system_prompt,
        client=client,
        tools=build_repo_setup_tools(conversation_id)
    )
