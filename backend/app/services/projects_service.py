from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.schemas.project import DocumentDetails, ProjectOverview
from app.services import project_store
from app.services.project_store import PROJECT_TABLE, REPOSITORY_TABLE
from app.utils.document_markdown import prd_json_to_markdown, trd_json_to_markdown


def _extract_document(
    doc_data: Any,
    updated_at: Optional[Any],
    doc_type: str,
) -> DocumentDetails:
    """Safely extracts DocumentDetails from Supabase JSONB or text column."""
    if not doc_data:
        return DocumentDetails(status="not_created", content=None, updated_at=None)

    content: Optional[str] = None
    if isinstance(doc_data, str):
        content = doc_data
    elif isinstance(doc_data, dict):
        if doc_type == "prd":
            content = prd_json_to_markdown(doc_data)
        else:
            content = trd_json_to_markdown(doc_data)
    else:
        content = str(doc_data)

    up_str = str(updated_at) if (updated_at is not None and str(updated_at).strip()) else None

    return DocumentDetails(
        status="created" if content else "not_created",
        content=content if content else None,
        updated_at=up_str if content else None,
    )


def get_projects_overview(user_id: str) -> List[ProjectOverview]:
    """Queries projects owned by user_id from Supabase and transforms them to ProjectOverview list."""
    if not user_id:
        return []

    try:
        supabase = project_store.client_factory()
    except Exception as exc:
        logger.warning(f"[ProjectsService] Supabase client unavailable: {exc}")
        return []

    # 1. Fetch conversations owned by this user to determine allowed conv_ids and human-readable titles
    try:
        conv_res = (
            supabase.table("Conversation")
            .select("conv_id, title")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        conv_rows = conv_res.data or []
    except Exception as exc:
        logger.error(f"[ProjectsService] Failed to query Conversation table for user '{user_id}': {exc}")
        return []

    if not conv_rows:
        logger.info(f"[ProjectsService] User '{user_id}' has no conversations; returning 0 projects.")
        return []

    conv_ids = [r["conv_id"] for r in conv_rows if r.get("conv_id")]
    if not conv_ids:
        return []

    titles_by_conv: Dict[str, str] = {
        r["conv_id"]: r["title"]
        for r in conv_rows
        if r.get("conv_id") and r.get("title")
    }

    # 2. Query Project rows matching only the user's conversations
    try:
        response = (
            supabase.table(PROJECT_TABLE)
            .select("*")
            .in_("conv_id", conv_ids)
            .order("created_at", desc=True)
            .execute()
        )
        rows = response.data or []
    except Exception as exc:
        logger.error(f"[ProjectsService] Failed to query Project table for user '{user_id}': {exc}")
        return []

    # Batch-fetch active repositories for any rows that don't have repo_url populated
    missing_repo_project_ids = [
        row["project_id"]
        for row in rows
        if not row.get("repo_url") and row.get("project_id")
    ]
    repo_by_project: Dict[str, str] = {}
    if missing_repo_project_ids:
        try:
            repo_res = (
                supabase.table(REPOSITORY_TABLE)
                .select("project_id, ado_repo_url, created_at")
                .in_("project_id", missing_repo_project_ids)
                .order("created_at", desc=True)
                .execute()
            )
            for repo_row in (repo_res.data or []):
                pid = repo_row.get("project_id")
                # Keep the latest created_at since rows are ordered desc
                if pid and pid not in repo_by_project and repo_row.get("ado_repo_url"):
                    repo_by_project[pid] = repo_row["ado_repo_url"]
        except Exception as err:
            logger.warning(f"[ProjectsService] Batch repository query failed: {err}")

    projects: List[ProjectOverview] = []
    for row in rows:
        conv_id = row.get("conv_id")
        if not conv_id:
            continue

        project_id = row.get("project_id")
        created_at = str(row.get("created_at") or "")
        updated_at = str(row.get("updated_at") or created_at)

        prd_raw = row.get("prd")
        trd_raw = row.get("trd")
        prd_details = _extract_document(prd_raw, updated_at, "prd")
        trd_details = _extract_document(trd_raw, updated_at, "trd")

        # Resolve human-readable project name
        project_name = row.get("project_name")
        c_title = titles_by_conv.get(conv_id, "").strip()
        if not project_name or project_name.strip() in ("Untitled Project", "New Project"):
            if c_title and c_title not in ("Untitled Project", "New Project", "what do you feel about", "I want to create a"):
                project_name = c_title
            elif isinstance(prd_raw, dict) and prd_raw.get("Objective"):
                obj_text = str(prd_raw["Objective"]).strip()
                if "library management" in obj_text.lower():
                    project_name = "Library Management System"
                elif "clinic" in obj_text.lower():
                    project_name = "Clinic Appointment Booking System"
                elif "attendance" in obj_text.lower() or "employee" in obj_text.lower():
                    project_name = "Attendance & Employee Management System"
                elif c_title:
                    project_name = c_title
                else:
                    project_name = "Enterprise Software Project"
            elif c_title:
                project_name = c_title
            else:
                project_name = "Enterprise Software Project"

        # Resolve human-readable project description
        description = row.get("project_description")
        if not description or description.strip() in ("No description provided yet.", ""):
            if isinstance(prd_raw, dict):
                description = prd_raw.get("Objective") or prd_raw.get("Problem Statement")
            if not description and c_title:
                description = f"Project specification and engineering workflow for {c_title}."

        project_link = row.get("ado_project_url")
        # Prefer repo_url on row; fall back to batch fetched repo mapping
        repo_link = row.get("repo_url") or repo_by_project.get(project_id)

        projects.append(
            ProjectOverview(
                conversation_id=conv_id,
                project_name=project_name,
                description=description,
                created_at=created_at,
                prd=prd_details,
                trd=trd_details,
                project_link=project_link,
                repo_link=repo_link,
            )
        )

    logger.info(f"[ProjectsService] Retrieved {len(projects)} projects for overview.")
    return projects


def update_project_name(conversation_id: str, new_name: str, user_id: str) -> ProjectOverview:
    """Updates the project name, keeping Project and Conversation tables consistent,
    and updating Azure DevOps if the project has already been provisioned in ADO.
    """
    clean_name = new_name.strip()
    if not clean_name:
        raise ValueError("Project name cannot be empty.")

    supabase = project_store.client_factory()

    # 1. Verify ownership of the conversation
    conv_res = (
        supabase.table("Conversation")
        .select("conv_id, title, user_id")
        .eq("conv_id", conversation_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not conv_res.data:
        raise ValueError(f"Conversation '{conversation_id}' not found or not owned by user.")

    # 2. Check existing Project row in Supabase
    project = project_store.get_project(conversation_id)
    ado_project_id = project.get("ado_project_id") if project else None
    new_ado_url: Optional[str] = None

    # 3. If an Azure DevOps project is already created, update it via ADO REST API
    if ado_project_id:
        from app.services.mcp.ado_mcp_server import update_ado_project
        ado_res = update_ado_project(project_id=str(ado_project_id), project_name=clean_name)
        if ado_res.get("status") != "success":
            err_msg = ado_res.get("message") or ado_res.get("details") or "Failed to rename Azure DevOps project."
            logger.error(f"[ProjectsService] ADO project rename failed: {err_msg}")
            raise RuntimeError(f"Azure DevOps project update failed: {err_msg}")
        new_ado_url = ado_res.get("url")

    # 4. Update the Project row in Supabase
    if project:
        update_fields: Dict[str, Any] = {"project_name": clean_name}
        if new_ado_url:
            update_fields["ado_project_url"] = new_ado_url
        project_store.update_project(project["project_id"], **update_fields)
    else:
        created_p = project_store.ensure_project(conversation_id)
        project_store.update_project(created_p["project_id"], project_name=clean_name)

    # 5. Update Conversation title in Supabase to keep names strictly consistent
    supabase.table("Conversation").update({"title": clean_name}).eq("conv_id", conversation_id).execute()
    logger.info(
        f"[ProjectsService] Synchronized project name & conversation title to '{clean_name}' for conv '{conversation_id}'"
        + (f" (ADO project '{ado_project_id}' updated)." if ado_project_id else ".")
    )

    # 6. Return the updated ProjectOverview
    overview_list = get_projects_overview(user_id)
    for p in overview_list:
        if p.conversation_id == conversation_id:
            return p

    raise RuntimeError("Failed to retrieve updated project overview.")

