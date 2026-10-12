from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.schemas.project import DocumentDetails, ProjectOverview
from app.services.conversation_service import get_supabase_client
from app.services.project_store import PROJECT_TABLE, REPOSITORY_TABLE
from app.utils.document_markdown import prd_json_to_markdown, trd_json_to_markdown


def _extract_document(
    doc_data: Any,
    updated_at: Optional[str],
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

    return DocumentDetails(
        status="created" if content else "not_created",
        content=content if content else None,
        updated_at=updated_at if content else None,
    )


def get_projects_overview() -> List[ProjectOverview]:
    """Queries all projects from Supabase and transforms them to ProjectOverview list."""
    try:
        supabase = get_supabase_client()
    except Exception as exc:
        logger.warning(f"[ProjectsService] Supabase client unavailable: {exc}")
        return []

    try:
        response = (
            supabase.table(PROJECT_TABLE)
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        rows = response.data or []
    except Exception as exc:
        logger.error(f"[ProjectsService] Failed to query Project table: {exc}")
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

    # Batch-fetch conversation titles to ensure every project has a human-readable title
    conv_ids = [row["conv_id"] for row in rows if row.get("conv_id")]
    titles_by_conv: Dict[str, str] = {}
    if conv_ids:
        try:
            conv_res = (
                supabase.table("Conversation")
                .select("conv_id, title")
                .in_("conv_id", conv_ids)
                .execute()
            )
            for c_row in (conv_res.data or []):
                cid = c_row.get("conv_id")
                ctitle = c_row.get("title")
                if cid and ctitle:
                    titles_by_conv[cid] = ctitle
        except Exception as err:
            logger.warning(f"[ProjectsService] Batch conversation query failed: {err}")

    projects: List[ProjectOverview] = []
    for row in rows:
        conv_id = row.get("conv_id")
        if not conv_id:
            continue

        project_id = row.get("project_id")
        created_at = row.get("created_at") or ""
        updated_at = row.get("updated_at") or created_at

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
