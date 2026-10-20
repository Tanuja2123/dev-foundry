import re
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional
from pydantic import BaseModel, Field
from agent_framework import Agent, FunctionInvocationContext, FunctionTool, function_middleware, tool
from agent_framework.openai import OpenAIChatCompletionClient
from app.services.mcp.ado_mcp_server import (
    create_ado_project,
    create_ado_repository,
    push_repository_changes,
    push_repository_files,
    update_ado_project,
)
from app.services.mcp.ado_git_push import AdoPushError, DEFAULT_COMMIT_MESSAGE, MAX_FILES, MAX_FILE_BYTES
from app.services import progress
from app.services.project_readme import README_PATH, build_readme
from app.services import project_store as store
from app.services.repo_structure import apply_structure_update, layer_changes, normalize_structure
from app.services.scaffold_service import MAX_COMMANDS, run_scaffold_commands, validate_scaffold_plan
from app.core.logger import logger

# The only operations in which each tool may run. The agent for an operation only receives these tools, so a model
# cannot create or push anything while it is asked for a proposal (it once created an ADO project called "placeholder").
TOOL_OPERATIONS: Dict[str, frozenset] = {
    "create_ado_project": frozenset({"CREATE_PROJECT"}),
    "update_ado_project": frozenset({"UPDATE_ADO_PROJECT"}),
    "create_ado_repository": frozenset({"CREATE_REPOSITORY"}),
    "scaffold_repository_structure": frozenset({"CREATE_REPOSITORY", "GENERATE_REPOSITORY_STRUCTURE",
                                                "UPDATE_REPOSITORY_STRUCTURE"}),
    "update_repository_structure": frozenset({"CREATE_REPOSITORY", "GENERATE_REPOSITORY_STRUCTURE",
                                              "UPDATE_REPOSITORY_STRUCTURE"}),
    "save_repository_structure": frozenset({"GENERATE_REPOSITORY_STRUCTURE", "UPDATE_REPOSITORY_STRUCTURE"}),
    "push_repository_structure": frozenset({"PUSH_REPOSITORY_STRUCTURE"}),
}

SCAFFOLD_COMMIT_MESSAGE = "chore: initial scaffold from generator commands"
CHANGES_COMMIT_MESSAGE = "feat: add requested project files"


class RepositoryFile(BaseModel):
    path: str = Field(description="File path from the repository root, e.g. /backend/app/auth/__init__.py")
    content: str = Field(description="Short, non-empty placeholder content that fits the stack. No application logic.")


class ScaffoldCommandInput(BaseModel):
    command: str = Field(description="ONE non-interactive generator / init / package-manager command, e.g. "
                                     "'npx --yes create-vite@latest frontend --template react-ts' or 'uv add --no-sync typer'. "
                                     "No '&&', pipes, redirects, quotes, environment variables or 'cd'.")
    cwd: str = Field(default=".", description="Folder relative to the repository root to run the command in "
                                              "(e.g. 'backend'); '.' is the repository root. Created if missing.")


class InitializerInput(BaseModel):
    name: str = Field(description="Online initializer: 'spring-boot', 'quarkus' or 'micronaut' (see the scaffold catalog).")
    folder: str = Field(default=".", description="Folder relative to the repository root for the project "
                                                 "(e.g. 'backend'); '.' is the repository root.")
    options: Dict[str, str] = Field(default_factory=dict,
                                    description="Initializer options as name/value strings, e.g. {\"build\": \"maven\", "
                                                "\"language\": \"java\", \"java_version\": \"21\", "
                                                "\"dependencies\": \"web,data-jpa,postgresql\"}.")


def scaffold_commit_message(commands: List[Dict[str, str]]) -> str:
    """Commit message of the generator-output commit: lists the commands (and the versions they used)."""
    lines = []
    for c in commands or []:
        line = f"$ {c['command']}" + (f"   (in {c['cwd']}/)" if c.get("cwd") not in (None, "", ".") else "")
        lines.append(line + (f"   [{c['version']}]" if c.get("version") else ""))
    return SCAFFOLD_COMMIT_MESSAGE + ("\n\nGenerated with:\n" + "\n".join(lines) if lines else "")


_DOC_FILE = re.compile(r"(^|/)(docs?/|README|CHANGELOG|CONTRIBUTING|LICENSE)|\.(md|mdx|rst|txt|adoc)$", re.IGNORECASE)
_CONFIG_FILE = re.compile(r"(^|/)\.[^/]+$|\.(json|ya?ml|toml|ini|cfg|conf|properties|lock|xml|gradle|kts|plist)$|"
                          r"(^|/)(Dockerfile|Makefile)$", re.IGNORECASE)


def _areas(paths: List[str], limit: int = 3) -> str:
    """'frontend/linux, frontend/macos and 2 more' – the folders (two levels deep) the changed paths are in."""
    areas: List[str] = []
    for path in paths:
        parts = path.strip("/").split("/")
        area = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "/".join(parts))
        if area not in areas:
            areas.append(area)
    shown = ", ".join(areas[:limit])
    return shown + (f" and {len(areas) - limit} more" if len(areas) > limit else "")


def changes_commit_message(base_files: List[Dict[str, Any]], files: List[Dict[str, Any]],
                           versions: Optional[str] = None) -> str:
    """
    A commit message that describes the commit (FEATURES SCAF-R11), built from the actual change set: a conventional
    type from what changed (only removals -> chore, docs -> docs, code -> feat, edits -> refactor), a subject with the
    areas and counts, and a body listing the counts and the structure version.
    """
    changes = layer_changes(base_files, files)
    added, edited, removed = changes.get("add") or [], changes.get("edit") or [], changes.get("delete") or []
    if not (added or edited or removed):
        return CHANGES_COMMIT_MESSAGE
    if (added or edited) and all(_DOC_FILE.search(p) for p in added + edited) and not removed:
        kind, verb, paths = "docs", ("add" if added and not edited else "update"), added + edited
    elif added and all(_DOC_FILE.search(p) or _CONFIG_FILE.search(p) for p in added) and not removed:
        kind, verb, paths = "chore", "add", added
    elif added:
        kind, verb, paths = "feat", "add", added
    elif removed and not edited:
        kind, verb, paths = "chore", "remove", removed
    else:
        kind, verb, paths = "refactor", "update", edited + removed
    count = len(paths)
    names = [p.lstrip("/") for p in paths]
    listed = names[0] if count == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    # Name the files when there are few (seen: "docs: add 2 files in README.md, docs"), else the areas
    subject = (f"{kind}: {verb} {listed}" if count <= 3 and len(listed) <= 60
               else f"{kind}: {verb} {count} files in {_areas(paths)}")
    body = [f"On top of the generator output: {len(added)} added, {len(edited)} changed, {len(removed)} removed."]
    if README_PATH in added + edited:
        body.append("README.md is written by DevFoundry from the generated files.")
    if versions:
        body.append(f"Structure {versions}.")
    return subject[:120] + "\n\n" + " ".join(body)


def _error(message: str, error_type: str = "STATE_ERROR", details: str = "") -> Dict[str, Any]:
    return {"status": "error", "error_type": error_type, "message": message, "details": details}


def build_repo_setup_tools(conversation_id: Optional[str], operation: Optional[str] = None,
                           deferred: Optional[Dict[str, Any]] = None) -> List[FunctionTool]:
    """
    Builds the Repo Setup Agent tools bound to one conversation. ADO results and the reviewed repository
    structure are persisted in the Project/Repository tables, so the structure shown to the user is exactly
    the structure that gets pushed (the push tool reads it from the database, never from the model).

    With `operation` (always passed by the chat pipeline), only the tools allowed for that operation are returned
    (none for proposals), and the create tools use the stored, user-approved names instead of the model's arguments.
    """
    gated = operation is not None

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
            # Idempotent: a repeated call (models sometimes call the tool twice in one turn) returns the existing
            # project as a success, so the reply never reports a failure for a project that was created.
            logger.info(f"[Repo_Setup_Agent] create_ado_project: '{project.get('project_name')}' already exists; "
                        "returning it (nothing new created)")
            return {"status": "success", "already_existed": True, "name": project.get("project_name"),
                    "description": project.get("project_description"), "id": project.get("ado_project_id"),
                    "url": project.get("ado_project_url"),
                    "message": "The Azure DevOps project for this conversation exists; nothing new was created."}

        approved_name = (project or {}).get("project_name")
        if not store.is_placeholder_name(approved_name):
            # The user approved the stored proposal: create exactly that, whatever the model passed
            if project_name and project_name.strip() != approved_name.strip():
                logger.warning(f"[Repo_Setup_Agent] create_ado_project called with '{project_name}', "
                               f"using the approved proposal '{approved_name}'")
            project_name = approved_name
            project_description = (project or {}).get("project_description") or project_description
        elif gated:
            return _error("There is no approved project proposal yet. Propose a project name and description first.",
                          "NO_APPROVED_PROPOSAL")
        if store.is_placeholder_name(project_name) or store.is_placeholder_name(project_description):
            return _error(f"'{project_name}' / '{project_description}' is not a real project name/description.",
                          "VALIDATION_ERROR")

        progress.begin_side_effect("Creating the Azure DevOps project")    # Stop can no longer cancel the turn
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
        if project_name is not None and store.is_placeholder_name(project_name):
            return _error(f"'{project_name}' is not a real project name.", "VALIDATION_ERROR")
        if project_description is not None and store.is_placeholder_name(project_description):
            project_description = None
        progress.begin_side_effect("Updating the Azure DevOps project")
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

    def _created_repository() -> Dict[str, Any]:
        repository = load_state()["repository"]
        if not repository or not repository.get("ado_repo_id"):
            raise store.StoreError("Create the repository before saving its folder structure.")
        return repository

    def _save(repository: Dict[str, Any], structure: List[Dict[str, str]], **scaffold: Any) -> Dict[str, Any]:
        """
        Stores a new structure version (tagged with the TRD version it reflects) and reopens the review.
        `scaffold` = scaffold_commands / scaffold_structure (only stored once migration 005 exists).
        """
        if progress.cancel_requested():
            # Stop was clicked while the generators ran in this worker thread: nothing is saved after the fact
            raise store.StoreError("The request was stopped, so the structure was not saved.")
        project = load_state()["project"] or {}
        if not store.has_scaffold_columns(repository):
            scaffold = {}
        row = store.save_structure(repository["repo_id"], structure, repository.get("structure_version") or 0,
                                   trd_version=project.get("trd_version") if "trd_version" in project else None,
                                   **scaffold)
        if project.get("project_id"):
            store.update_project(project["project_id"], workflow_stage=store.STAGE_STRUCTURE_REVIEW)
        return row

    def _structure_result(row: Dict[str, Any]) -> Dict[str, Any]:
        files = row.get("structure") or []
        result = {"status": "success", "repository_name": row.get("repo_name"),
                  "structure_version": row.get("structure_version"), "files_count": len(files),
                  "paths": [f["path"] for f in files]}
        if row.get("scaffold_structure"):
            result["changes_on_top_of_generated_files"] = layer_changes(row["scaffold_structure"], files)
        return result

    @tool(
        name="create_ado_repository",
        description="Create a new, EMPTY Git repository in the Azure DevOps project using the approved repository name. "
                    "It creates no files: generate the structure afterwards with scaffold_repository_structure."
    )
    def tool_create_ado_repository(project_id_or_name: str, repository_name: str) -> Dict[str, Any]:
        """
        TOOL 2 — ADO REPOSITORY CREATION
        Calls the ADO MCP Server to create an empty Git repository within the Azure DevOps project.
        """
        try:
            state = load_state()
        except store.StoreError as e:
            return _error(str(e))
        project, repository = state["project"], state["repository"]
        if not project or not project.get("ado_project_id"):
            return _error("Create the Azure DevOps project before creating a repository.")
        if repository and repository.get("ado_repo_id") and repository.get("repo_name") == repository_name:
            # Idempotent, like create_ado_project: a repeated call returns the existing repository as a success
            logger.info(f"[Repo_Setup_Agent] create_ado_repository: '{repository_name}' already exists; returning it")
            return {"status": "success", "already_existed": True, "name": repository_name,
                    "id": repository.get("ado_repo_id"), "url": repository.get("ado_repo_url"),
                    "message": f"Repository '{repository_name}' exists; nothing new was created."}
        proposed = repository.get("repo_name") if repository and repository.get("status") == store.REPO_PROPOSED else None
        if not store.is_placeholder_name(proposed):
            if repository_name and repository_name.strip() != proposed:
                logger.warning(f"[Repo_Setup_Agent] create_ado_repository called with '{repository_name}', "
                               f"using the approved proposal '{proposed}'")
            repository_name = proposed
        elif gated:
            return _error("There is no approved repository name yet. Propose a repository name first.",
                          "NO_APPROVED_PROPOSAL")
        if store.is_placeholder_name(repository_name):
            return _error(f"'{repository_name}' is not a real repository name.", "VALIDATION_ERROR")

        progress.begin_side_effect("Creating the repository in Azure DevOps")
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

    @tool(
        name="save_repository_structure",
        description=(
            "Save a COMPLETE hand-written file list as the repository structure. ONLY when the user explicitly supplies the "
            "complete list themselves or explicitly asks for a structure without generator commands; otherwise use "
            "scaffold_repository_structure. Replaces the saved structure. Nothing is pushed. "
            f"Limits: {MAX_FILES} files, {MAX_FILE_BYTES} bytes per file."
        )
    )
    def tool_save_repository_structure(
        files: Annotated[List[RepositoryFile], Field(description="List of skeleton files (12-25 files) with their 'path' fields.")],
    ) -> Dict[str, Any]:
        """TOOL 3 — SAVE REPOSITORY STRUCTURE (validated, stored in the Repository table)."""
        try:
            repository = _created_repository()
            structure = normalize_structure(files)
            return _structure_result(_save(repository, structure, scaffold_commands=None, scaffold_structure=None))
        except AdoPushError as e:
            return _error(e.message, e.error_type)
        except store.StoreError as e:
            return _error(str(e))

    @tool(
        name="scaffold_repository_structure",
        description=(
            "Generate the repository structure with the stack's OFFICIAL generators: online initializers (Spring Boot, "
            "Quarkus, Micronaut; run first) and/or CLI generator commands (in order, non-interactively, without a shell), in "
            "an empty temporary folder that is the repository root. Saves EXACTLY the files they produce for review "
            "(replaces the saved structure; nothing is pushed). Returns the generated paths and any warnings, or an error "
            f"with the output so the call can be fixed. At most {MAX_COMMANDS} steps in total."
        )
    )
    def tool_scaffold_repository_structure(
        commands: Annotated[Optional[List[ScaffoldCommandInput]],
                            Field(description="CLI generator commands in execution order (run after the initializers).")] = None,
        initializers: Annotated[Optional[List[InitializerInput]],
                                Field(description="Online initializers (no local toolchain needed), run first.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 3b — SCAFFOLD REPOSITORY STRUCTURE (runs the commands, stores their exact output)."""
        try:
            _created_repository()
        except store.StoreError as e:
            return _error(str(e))
        if deferred is None:
            return run_scaffold_plan(commands, initializers)
        # AGT-29: the commands take minutes; they run after the model's turn, not inside it (the model would wait,
        # the stall watchdog fired and the structure was saved after the reply). Validate now, run later.
        if deferred.get("run"):
            return {"status": "success", "planned": True,
                    "message": "The generator commands are already planned for this turn; finish with your JSON."}
        try:
            validate_scaffold_plan(commands or [], initializers or [])
        except AdoPushError as e:
            return _error(e.message, e.error_type, e.details)
        deferred["run"] = lambda: run_scaffold_plan(commands, initializers)
        return {"status": "success", "planned": True,
                "commands": [getattr(c, "command", None) or str(c) for c in commands or []],
                "message": ("The commands are valid. They run right after this step (a few minutes) and the system "
                            "then shows the generated structure to the user. Do not call scaffold_repository_structure "
                            "or update_repository_structure again in this turn: finish with your JSON (status "
                            "SUCCESS).")}

    def run_scaffold_plan(commands: Optional[List[Any]], initializers: Optional[List[Any]]) -> Dict[str, Any]:
        """Runs the generator commands, stores their exact output, and re-adds the files the user had added."""
        try:
            repository = _created_repository()
        except store.StoreError as e:
            return _error(str(e))
        previous = repository.get("scaffold_structure")
        previous_changes = layer_changes(previous, repository.get("structure") or []) if previous else None
        try:
            result = run_scaffold_commands(commands or [], initializers=initializers or [],
                                           default_name=repository.get("repo_name") or "app")
        except AdoPushError as e:
            return _error(e.message, e.error_type, e.details)
        command_list = [c.to_dict() for c in result.commands]
        files = result.files
        # A regeneration keeps the files the user ADDED (e.g. docs/architecture.md) when their path is free; edits to
        # generated files can't be carried over (the generated files changed) and are reported instead
        kept: List[str] = []
        if previous_changes and previous_changes.get("add"):
            current = {f["path"]: f for f in repository.get("structure") or []}
            taken = {f["path"] for f in files}
            extra = [current[p] for p in previous_changes["add"] if p in current and p not in taken]
            if extra:
                files = files + extra
                kept = [f["path"] for f in extra]
        # A root README that says how to set the project up and run it (FEATURES SCAF-R12): written by code from the
        # generated files; it is a requested change (commit 2), so the generator commit stays pure output
        project = load_state()["project"] or {}
        readme = build_readme(project.get("project_name") or repository.get("repo_name") or "Project",
                              project.get("project_description"), files)
        if readme:
            files = [f for f in files if f["path"] != README_PATH] + [{"path": README_PATH, "content": readme}]
        try:
            row = _save(repository, files, scaffold_commands=command_list, scaffold_structure=result.files)
        except store.StoreError as e:
            return _error(str(e))
        response = _structure_result(row)
        response.update(commands=command_list, duration_seconds=result.duration_seconds)
        if result.skipped:
            response["skipped"] = result.skipped
        if result.warnings:
            response["warnings"] = result.warnings
        if kept:
            response["kept_requested_files"] = kept
        dropped = [p for key in ("edit", "delete") for p in (previous_changes or {}).get(key) or []]
        if dropped:
            response["note"] = ("The structure was regenerated, so earlier changes to generated files are no longer "
                                "applied: " + ", ".join(dropped[:10]) + ".")
        if not store.has_scaffold_columns(repository):
            response["warning"] = ("Migration 005 (scaffold columns) is not applied: the generator output and the "
                                   "requested changes will be pushed as one commit.")
        return response

    @tool(
        name="update_repository_structure",
        description=(
            "Apply ONLY the user's explicitly requested changes on top of the saved (generated) structure. Only the listed "
            "files are added/replaced and only the listed paths are removed (a folder path removes everything under it); "
            "all other files - including the generator output - stay exactly as they are."
        )
    )
    def tool_update_repository_structure(
        upsert_files: Annotated[Optional[List[RepositoryFile]], Field(description="Files to add or replace.")] = None,
        remove_paths: Annotated[Optional[List[str]], Field(description="File or folder paths to remove.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 4 — UPDATE REPOSITORY STRUCTURE (applies a change set to the stored structure)."""
        if deferred is not None and deferred.get("run"):
            return _error("The structure is generated right after this step; further changes can be requested in the "
                          "next message.", "PLANNED")
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
            "Push the saved, user-approved structure to the repository's main branch. First push: commit 1 contains the "
            "generator output exactly as the commands produced it, commit 2 (only if the user requested changes) contains "
            "those changes. Later pushes: one follow-up commit with only the differences. The files come from the saved "
            "structure (exactly what the user reviewed); do not pass files."
        )
    )
    def tool_push_repository_structure(
        commit_message: Annotated[Optional[str], Field(description="Commit message for the user's changes (or for the single commit). Omit to use the default.")] = None,
    ) -> Dict[str, Any]:
        """TOOL 5 — PUSH REPOSITORY STRUCTURE (reads the approved structure from the database and pushes it)."""
        result = push_structure(commit_message)
        if deferred is not None:
            deferred["push"] = result          # the reply is built by code from the real commits (ISSUES AGT-49)
        return result

    def push_structure(commit_message: Optional[str] = None) -> Dict[str, Any]:
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
        repo_name = repository["repo_name"]
        previously_pushed = repository.get("pushed_structure")
        unchanged = previously_pushed is not None and repo_structure_key(previously_pushed) == repo_structure_key(structure)
        if unchanged or (previously_pushed is None and repository.get("status") == store.REPO_PUSHED):
            # Nothing new since the last push: a success that says so, never an error (seen live: a repeated call's
            # "NO_CHANGES" error made the reply report a failed push although both commits were in Azure DevOps)
            logger.info(f"[Repo_Setup_Agent] push_repository_structure: '{repo_name}' is already up to date "
                        f"(commit {repository.get('commit_id')}); nothing new to push")
            return {"status": "success", "already_pushed": True, "repository_name": repo_name,
                    "commit_id": repository.get("commit_id"), "branch": repository.get("branch"),
                    "structure_version": repository.get("structure_version"),
                    "message": "Already pushed: the repository is up to date and there is nothing new to push."}

        def record(result: Dict[str, Any], pushed: List[Dict[str, str]]) -> Optional[str]:
            fields = {"status": store.REPO_PUSHED, "commit_id": result.get("commit_id"),
                      "commit_message": result.get("commit_message"), "branch": result.get("branch"),
                      "pushed_at": datetime.now(timezone.utc).isoformat()}
            if "pushed_structure" in repository:
                fields.update(pushed_structure=pushed, pushed_structure_version=repository.get("structure_version"))
            try:
                store.update_repository(repository["repo_id"], **fields)
                if pushed == structure:
                    store.update_project(project["project_id"], workflow_stage=store.STAGE_COMPLETE)
            except store.StoreError as e:
                return f"Structure pushed but not saved: {e}"
            return None

        def summary(result: Dict[str, Any], kind: str) -> Dict[str, Any]:
            item = {"kind": kind, "commit_id": result.get("commit_id"), "commit_message": result.get("commit_message"),
                    "files_count": result.get("files_count"), "commit_url": result.get("commit_url")}
            if result.get("changes"):
                item["changes"] = result["changes"]
            return item

        progress.begin_side_effect("Pushing to Azure DevOps")
        scaffold = repository.get("scaffold_structure")
        commands = repository.get("scaffold_commands") or []
        if previously_pushed is None and scaffold and "pushed_structure" in repository:
            # 1) the generator output exactly as the commands produced it
            requested = layer_changes(scaffold, structure)
            has_changes = any(requested.values())
            first_message = commit_message if (commit_message and not has_changes) else scaffold_commit_message(commands)
            first = push_repository_files(project_id_or_name=project_ref, repository_name=repo_name, files=scaffold,
                                          commit_message=first_message, allow_empty=True)
            if first.get("status") != "success":
                logger.error(f"[Repo_Setup_Agent] Scaffold push failed: {first.get('message')}")
                return first
            warning = record(first, scaffold)
            commits = [summary(first, "generator output")]
            result = dict(first)
            # 2) the user's requested changes on top of it, through the regular change push
            if has_changes:
                second = push_repository_changes(project_id_or_name=project_ref, repository_name=repo_name,
                                                 base_files=scaffold, files=structure,
                                                 commit_message=commit_message or changes_commit_message(
                                                     scaffold, structure, f"v{repository.get('structure_version')}"),
                                                 branch=first.get("branch"), allow_empty=True)
                if second.get("status") != "success":
                    logger.error(f"[Repo_Setup_Agent] Requested-changes push failed: {second.get('message')}")
                    second["generator_commit"] = commits[0]
                    second["message"] = ("The generator output was pushed as the initial commit, but pushing the requested "
                                         "changes failed: " + str(second.get("message")))
                    return second
                warning = record(second, structure) or warning
                commits.append(summary(second, "requested changes"))
                result = dict(second)
            result.update(commits=commits, files_count=len(structure), structure_version=repository.get("structure_version"),
                          push_type="initial commit" + (" + requested changes commit" if has_changes else ""),
                          commands=commands)
            if warning:
                result["warning"] = warning
            return result

        if previously_pushed is not None:
            result = push_repository_changes(project_id_or_name=project_ref, repository_name=repo_name,
                                             base_files=previously_pushed, files=structure,
                                             commit_message=commit_message or changes_commit_message(
                                                 previously_pushed, structure,
                                                 f"v{repository.get('pushed_structure_version')} -> v{repository.get('structure_version')}"),
                                             branch=repository.get("branch"),
                                             allow_empty=True)
        else:
            result = push_repository_files(project_id_or_name=project_ref, repository_name=repo_name,
                                           files=structure, commit_message=commit_message, allow_empty=True)
        if result.get("status") == "success":
            result["structure_version"] = repository.get("structure_version")
            result["push_type"] = "follow-up commit" if previously_pushed is not None else "initial commit"
            result["commits"] = [summary(result, result["push_type"])]
            warning = record(result, structure)
            if warning:
                result["warning"] = warning
        else:
            logger.error(f"[Repo_Setup_Agent] Structure push failed: {result.get('message')}")
        return result

    tools = [tool_create_ado_project, tool_update_ado_project, tool_create_ado_repository, tool_save_repository_structure,
             tool_scaffold_repository_structure, tool_update_repository_structure, tool_push_repository_structure]
    if gated:
        tools = [t for t in tools if operation in TOOL_OPERATIONS.get(t.name, frozenset())]
    return tools


def get_repo_setup_agent(client: OpenAIChatCompletionClient, conversation_id: Optional[str] = None,
                         operation: Optional[str] = None, deferred: Optional[Dict[str, Any]] = None) -> Agent:
    """
    Creates and returns the Repo Setup Agent using Microsoft Agent Framework. With `operation`, the agent only gets
    the tools that operation allows (see TOOL_OPERATIONS); proposal operations get no tools at all.
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
  were created, the generator commands of the saved structure (scaffold_commands), every saved file with its origin
  ([generated] = generator output, [added] / [edited] = requested changes). Structure operations also get a
  "[Scaffold catalog]" block: the ONLY generators that can run (CLI generators with reference commands, online initializers)
  and the stacks whose toolchain is NOT installed on the server.
- The PRD and TRD can be updated by the user at any time (by the Requirements_Agent). The state block lists "outdated"
  items when an artifact no longer reflects the latest documents (e.g. ADO_PROJECT_OUTDATED, STRUCTURE_OUTDATED).
  Always base names, descriptions and structures on the LATEST approved_prd_json / approved_trd_json.

==================================================
3. TOOL BOUNDARY (EXACTLY SEVEN TOOLS)
==================================================
1. create_ado_project(project_name, project_description): creates the ADO project.
2. update_ado_project(project_name?, project_description?): renames / re-describes the ALREADY CREATED ADO project.
3. create_ado_repository(project_id_or_name, repository_name): creates an EMPTY Git repository (no files).
4. scaffold_repository_structure(commands?: [{command, cwd}], initializers?: [{name, folder, options}]): RUNS the stack's
   official generators (online initializers first, then CLI commands) in an empty temporary folder that is the repository
   root and saves EXACTLY the files they produce as the structure for review (replaces the saved structure). Returns the
   generated paths and warnings, or an error with the output.
5. update_repository_structure(upsert_files, remove_paths): applies ONLY the user's explicitly requested changes on top of the
   generated structure (everything else stays exactly as generated).
6. save_repository_structure(files): saves a hand-written COMPLETE file list. ONLY when the user explicitly supplies the
   complete list themselves or explicitly asks for a structure without generator commands.
7. push_repository_structure(commit_message?): pushes the saved structure to the main branch. First push: commit 1 = the
   generator output exactly as produced, commit 2 = the requested changes (only if there are any). After that: one
   follow-up commit with only the differences.

Rules:
- You only receive the tools the current operation allows (proposal operations receive none). Never claim that something
  was created, changed or pushed unless a tool in THIS request returned success for it.
- create_ado_project / create_ado_repository always use the name the user approved (the stored proposal); never invent
  names or descriptions, and never use placeholders such as "placeholder", "<project name>" or "TBD".
- NEVER call create_ado_project without an explicit CREATE_PROJECT operation from the Orchestrator.
- NEVER call create_ado_repository without an explicit CREATE_REPOSITORY operation from the Orchestrator.
- The base structure ALWAYS comes from scaffold_repository_structure (see "5. SCAFFOLD RULES"). NEVER write starter,
  config, manifest or framework files yourself, and never add files the user did not explicitly ask for.
- Call update_repository_structure only for changes the user explicitly asked for (files, folders, modules, CI workflows...).
- Call update_ado_project only during UPDATE_ADO_PROJECT.
- NEVER call push_repository_structure without an explicit PUSH_REPOSITORY_STRUCTURE operation (the user approved the shown structure).
- If scaffold_repository_structure returns COMMAND_NOT_ALLOWED, COMMAND_FAILED, COMMAND_TIMEOUT, VALIDATION_ERROR or
  INITIALIZER_FAILED, read the message and output, fix the call and try again (at most 2 retries). If it returns warnings
  (expected files missing), check the options and fix them the same way. INITIALIZER_UNAVAILABLE: report it (the service is
  down), do not switch stacks on your own. NEVER fall back to writing the files yourself.
- NEVER replace the stack the user / TRD chose with another one on your own. If the chosen stack cannot be generated here
  (its toolchain is listed as NOT available, or TOOL_NOT_INSTALLED), do not scaffold: return STRUCTURE_OPTIONS (section 6)
  and let the user decide.
- If save/update returns a VALIDATION_ERROR, fix the files and call it again (at most 2 retries). Never retry a failed create or push.
- If any tool returns status "error", report it as an error. NEVER claim success for a step whose tool returned an error.
- NEVER print the repository structure tree yourself; the system renders it from the saved structure.
- NEVER expose credentials, tokens, or PATs in responses or logs.
- AZURE DEVOPS PROJECT NAME RULES: use letters, numbers, spaces and hyphens (max 64 characters). NEVER use '&' or other special
  characters; write 'and' instead (e.g. 'Git Changelog and Release Notes CLI', NEVER 'Git Changelog & Release Notes CLI').

==================================================
4. WORKFLOW STAGES & OPERATIONS
==================================================
The operations supported are:

A. GENERATE_PROJECT_PROPOSAL:
- Triggered when moving to repository setup after TRD/PRD completion.
- Analyze PRD, TRD, tech stack, and project context.
- Suggest a concise, descriptive ADO project name and clear project description reflecting the actual software project.
- ADO Project Name Rule: Must use ONLY letters, numbers, spaces, and hyphens (max 64 chars). NEVER use '&' or punctuation; always write 'and'.
- Return status "COMPLETE", workflow_stage "PROJECT_REVIEW", with allowed_operations: ["UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT"].
- DO NOT invoke any tool during this operation.

B. UPDATE_PROJECT_PROPOSAL:
- Triggered when the user requests changes to the project name, description, or both.
- Modify only the requested portions while preserving valid existing information.
- ADO Project Name Rule: Must use ONLY letters, numbers, spaces, and hyphens (max 64 chars). NEVER use '&' or punctuation; always write 'and'.
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
  NEVER create a repository during this operation.

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
- If it succeeds (or if the repository already exists / already_existed is True):
  * If the repository structure does not exist yet: call `scaffold_repository_structure` with the generators for the approved TRD (see section 5). If the TRD's stack cannot be generated on this server, do not call it: return STRUCTURE_OPTIONS (the repository stays empty until the user chooses).
  * If the repository structure is already scaffolded in the workflow state: include that structure in the output.
  * In all cases, return the proposed repository structure so the user can review it before pushing.
- Return status "COMPLETE", workflow_stage "REPOSITORY_CREATED", repository details (name, id, url), the structure (version,
  files_count, the commands exactly as run, and a summary of what the generators created), and next_action state
  "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].
- If create_ado_repository returns an error: return status "ERROR", workflow_stage "REPOSITORY_CREATION", error details, and
  next_action state "REPOSITORY_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY"].
- If the repository was created but scaffolding still fails after the retries: return status "ERROR", workflow_stage
  "STRUCTURE_REVIEW", the repository details, the error (message + the failing command), and next_action state "STRUCTURE_REVIEW"
  with allowed_operations: ["GENERATE_REPOSITORY_STRUCTURE"].

G. GENERATE_REPOSITORY_STRUCTURE:
- Triggered when the repository exists and the user wants a fresh structure, does not accept the generated approach, or asks
  for a different stack, framework, generator or template.
- Take the user's message into account together with the LATEST TRD: pick the commands that fit (the TRD is the source of truth
  for technologies; if the user names a technology the TRD does not have, still follow the user and mention in the summary
  that the TRD should be updated to match).
- If the user just chose one of the STRUCTURE_OPTIONS offered earlier, use exactly that option.
- If the requested stack cannot be generated on this server, return STRUCTURE_OPTIONS instead of scaffolding.
- Call `scaffold_repository_structure` ONCE (it replaces the saved structure). It validates the commands and returns
  "planned": the system runs them right after your turn, keeps the files the user added earlier, and shows the result.
  Do not call any other structure tool in the same turn; extra files the user asks for now are added in the next message.
- Return status "COMPLETE", workflow_stage "STRUCTURE_REVIEW", the structure (version, files_count, commands, summary), and
  next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

H. UPDATE_REPOSITORY_STRUCTURE:
- Triggered when the user requests changes to the shown structure (add, rename, move or remove files/folders, add modules such
  as auth, db, audit, CI workflows), or asks to update the structure to the latest TRD (STRUCTURE_OUTDATED). Also allowed after
  the structure was pushed: the changes are then pushed later as a follow-up commit.
- If the requested change is really a different stack, framework or generator (or STRUCTURE_OUTDATED because the TRD's
  languages/frameworks/app type no longer match the scaffold_commands), regenerate exactly as in G.
- Otherwise call `update_repository_structure` with ONLY the requested changes (a rename/move = remove the old path + add the
  new file). Keep the generated files unchanged unless the user asked to change or remove them. New files: short, non-empty
  placeholder content that fits the stack's conventions (e.g. a module docstring, a TODO comment, a minimal workflow).
  New Python packages need an __init__.py; never add files to keep folders that already contain files.
- Return status "COMPLETE", workflow_stage "STRUCTURE_REVIEW", the structure (version, files_count, commands, summary of what
  changed), and next_action state "STRUCTURE_REVIEW" with allowed_operations: ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

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
- If the tool succeeds: return status "COMPLETE", workflow_stage "STRUCTURE_PUSHED", the push details exactly as returned
  (push_type, commits [each with kind, commit_id, commit_message, files_count, commit_url, changes], branch, files_count,
  commands), and next_action state "COMPLETE" with allowed_operations: [].
- If the tool returns an error: return status "ERROR", workflow_stage "STRUCTURE_PUSH", error details (including
  generator_commit if the first commit was pushed), and next_action state "STRUCTURE_REVIEW" with allowed_operations:
  ["UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"].

==================================================
5. SCAFFOLD RULES
==================================================
- Read approved_trd_json (and the PRD when given): the kind of application (CLI tool, web frontend, API/service, full stack,
  library, mobile, desktop, infrastructure) and its languages/frameworks. If the TRD leaves something open, choose the stack's
  most common default.
- Use ONLY the generators in the "[Scaffold catalog]" block, with their reference commands / options; the user may choose ANY
  stack, so follow the TRD's stack even if another one would be simpler. You only choose generators; you never write the files.
- Java / Kotlin / Groovy (Spring Boot, Quarkus, Micronaut) use the online `initializers` argument (no JDK needed), e.g.
  {"name": "spring-boot", "folder": "backend", "options": {"build": "maven", "language": "java", "java_version": "21",
  "dependencies": "web,data-jpa,postgresql,validation"}}. Leave boot/platform versions out to get the latest stable one.
- The temporary folder IS the repository root:
  * ONE application (e.g. a CLI tool, a single API, a single web app, a library): generate directly into the root ('.').
    Do NOT create frontend/ or backend/ folders.
  * SEVERAL parts (e.g. React frontend + Spring Boot backend): one folder per part named after its role (frontend/ and
    backend/, or web/ and api/), each created by its own generator; run follow-up commands for a part with cwd = its folder.
- Commands run without a terminal; the system adds the known headless / skip-install flags automatically, but still pass every
  option a generator would otherwise ask for (template, language, name ...). Never install dependencies into node_modules or a
  virtualenv: prefer `uv add --no-sync`, `npm install --package-lock-only`.
- Runtime libraries the TRD names that the generator did not add (an ORM, a validation or auth library) are added with
  the package manager (`uv add --no-sync typer`, `npm install commander --package-lock-only`, `dotnet add package
  <name>`) or the initializer's dependencies option, never by editing files. Do NOT add test, lint, formatting or other
  dev tools (Vitest, Jest, Playwright, Prettier, pytest...) unless the user explicitly asks for them in this
  conversation: the generators already include what the template needs, and extra dev tools conflict with the
  template's pinned versions (seen: Vitest needing @types/node 22 while Next.js pins 20). Use the plain package name
  (never a pre-release, rc, beta or canary tag).
- ONE command per entry: no '&&', pipes, redirects, quotes, environment variables or 'cd' (use cwd). No version ranges with
  '<' / '>'; use the plain package name, an extra (fastapi[standard]) or '==<version>'. No URLs or absolute paths. Always use
  the latest generator versions (`@latest`); never pin old versions.
- After the tool returns, check the generated paths and warnings: if the result misses the TRD's main framework or put a single
  application into a subfolder, fix the call and run the tool again.

==================================================
6. OUTPUT CONTRACT (STRICT JSON ONLY)
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
    "commands": ["<each command exactly as run, with its folder if not the root>"],
    "summary": "<one or two sentences on what the generators created and how it fits the tech stack>"
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
    "commands": ["<the generator commands of the structure>"],
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
    "branch": "<branch>",
    "files_count": <number>,
    "push_type": "<as returned>",
    "commits": [
      {"kind": "<generator output | requested changes | follow-up commit | initial commit>", "commit_id": "<id>",
       "commit_message": "<message>", "files_count": <number>, "commit_url": "<url>",
       "changes": {"add": ["<path>"], "edit": ["<path>"], "delete": ["<path>"]}}
    ]
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

--- For a stack that cannot be generated here (STRUCTURE_OPTIONS) ---
{
  "status": "COMPLETE",
  "workflow_stage": "STRUCTURE_OPTIONS",
  "requested_stack": "<the stack the TRD / user asked for>",
  "reason": "<why it cannot be generated here, e.g. the Flutter SDK is not installed on the DevFoundry server>",
  "options": [
    {"label": "<short label>", "description": "<what would be generated and how (generator / initializer)>"}
  ],
  "next_action": {
    "state": "STRUCTURE_REVIEW",
    "allowed_operations": ["GENERATE_REPOSITORY_STRUCTURE"]
  }
}
Options: 2-4 of the closest stacks that CAN be generated (same language family / same kind of application first), plus,
always last, {"label": "Install the toolchain", "description": "an administrator installs <tool> on the DevFoundry server,
then ask again"}.

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
        tools=build_repo_setup_tools(conversation_id, operation, deferred),
        middleware=[tool_run_middleware()],
    )


def repo_structure_key(files: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """path -> (content, encoding): two structures with the same key push the same files."""
    return {f.get("path"): (f.get("content"), f.get("encoding")) for f in files or [] if isinstance(f, dict)}


# Tools whose action must happen at most once per turn (seen live: the model called push_repository_structure a second
# time two minutes after a successful push; the repeat returned an error and the reply could claim the push failed)
ONCE_PER_TURN_TOOLS = frozenset({"create_ado_project", "create_ado_repository", "push_repository_structure"})


def _succeeded(result: Any) -> bool:
    text = str(result)
    return "'status': 'success'" in text or '"status": "success"' in text


def tool_run_middleware():
    """
    Agent Framework function middleware, one per agent (= per turn):
    - marks a tool as running (progress.tool_started / tool_finished), so the stream watchdog doesn't take the time a
      generator or a push needs for a silent model;
    - a successful once-per-turn action called again in the same turn returns the first result instead of running again.
    """
    done: Dict[str, Any] = {}

    @function_middleware
    async def track_tool_runs(context: FunctionInvocationContext, call_next) -> None:
        name = getattr(context.function, "name", "")
        if name in done:
            logger.warning(f"[Repo_Setup_Agent] {name} was already done in this turn; returning that result instead "
                           "of running it again")
            context.result = done[name]
            return
        progress.tool_started()
        try:
            await call_next()
        finally:
            progress.tool_finished()
        if name in ONCE_PER_TURN_TOOLS and _succeeded(context.result):
            done[name] = context.result

    return track_tool_runs
