"""
Azure Boards work items from the PRD (FEATURES.md ADO-1): the three Backlog_Agent operations.

GENERATE_WORK_ITEMS_PROPOSAL / UPDATE_WORK_ITEMS_PROPOSAL: the Backlog agent drafts / changes the backlog; code validates
it against the PRD (every requirement exactly once), stores it and renders the preview (no synthesizer, so the preview
always matches what will be created).
CREATE_WORK_ITEMS: code only. Creates / updates the work items in the project's Azure Boards (idempotent by key).
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from agent_framework import Message

from app.core.logger import logger
from app.services import ado_boards
from app.services import progress
from app.services import project_store as store
from app.services.agents.backlog_agent import get_backlog_agent

MIGRATION_006_MESSAGE = ("Work items need a one-time database update first: run "
                         "`backend/db/migrations/006_work_items.sql` in the Supabase SQL editor, then ask again.")
NO_ADO_PROJECT_MESSAGE = ("Work items are created in the project's Azure Boards, and the Azure DevOps project doesn't "
                          "exist yet. Create the project first, then ask me to plan the work items.")
NO_DRAFT_MESSAGE = "There is no work items draft yet. Ask me to plan the work items from the PRD first."
DRAFT_FAILED_MESSAGE = ("I couldn't draft the work items this time, so nothing was changed. Please ask again in a "
                        "moment.")
GENERIC_TYPES = {"epic": "Epic", "feature": "Feature", "story": "Story"}


def _parse(text: str) -> Optional[Dict[str, Any]]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("backlog"), dict) else None


def _draft_for_agent(project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    draft = project.get("work_items_proposal")
    if not draft:
        return None
    return {key: value for key, value in draft.items() if key != "synced"}


async def run_backlog_operation(client: Any, operation: str, user_query: str, message_list: List[Any],
                                state: Dict[str, Any]) -> str:
    project = state.get("project") or {}
    if not store.has_work_item_columns(project):
        return MIGRATION_006_MESSAGE
    if not project.get("ado_project_id"):
        return NO_ADO_PROJECT_MESSAGE
    if operation == "CREATE_WORK_ITEMS":
        return await create_work_items(project)
    return await draft_work_items(client, operation, user_query, message_list, project)


async def draft_work_items(client: Any, operation: str, user_query: str, message_list: List[Any],
                           project: Dict[str, Any]) -> str:
    prd = project.get("prd") or {}
    current = _draft_for_agent(project)
    if operation == "UPDATE_WORK_ITEMS_PROPOSAL" and not current:
        operation = "GENERATE_WORK_ITEMS_PROPOSAL"
    context = (f"\n[Workflow Context from Orchestrator: Operation = {operation}]\n"
               f"approved_prd_json: {json.dumps(prd, ensure_ascii=False)}")
    if current:
        context += f"\ncurrent_work_items_draft_json: {json.dumps(current, ensure_ascii=False)}"
    recent = message_list[:-1]          # the whole conversation (already limited to the token budget)
    messages = recent + [Message(role="user", contents=[user_query + context])]

    agent = get_backlog_agent(client)
    logger.info(f"[Backlog_Agent] {operation}: {len(ado_boards.prd_requirements(prd))} PRD requirements")
    response = await agent.run(messages=messages)
    raw = response.messages[-1].text if response.messages else ""
    data = _parse(raw)
    if data is None:                     # one repair attempt (seen with fallback models)
        logger.warning("[Backlog_Agent] Output is not the expected JSON; asking again")
        response = await agent.run(messages=messages + [
            Message(role="assistant", contents=[raw]),
            Message(role="user", contents=["Your previous answer is not the required JSON object. Return the same "
                                           "answer again as ONE valid JSON object following the OUTPUT shape."])])
        data = _parse(response.messages[-1].text if response.messages else "")
    if data is None:
        logger.error("[Backlog_Agent] No usable backlog JSON after the repair attempt")
        return DRAFT_FAILED_MESSAGE

    backlog, notes = ado_boards.normalize_backlog(data["backlog"], prd)
    if notes:
        logger.warning(f"[Backlog_Agent] Draft corrected: {'; '.join(notes)}")
    prd_version = project.get("prd_version") or 0
    store.update_project(project["project_id"], work_items_proposal=backlog,
                         work_items_proposal_prd_version=prd_version)
    features, stories = ado_boards.backlog_counts(backlog)
    logger.info(f"[Backlog_Agent] Draft saved: {features} features, {stories} stories (PRD v{prd_version})")
    return render_draft_reply(project, backlog, operation, data.get("change_summary"))


def render_draft_reply(project: Dict[str, Any], backlog: Dict[str, Any], operation: str,
                       change_summary: Optional[str] = None) -> str:
    features, stories = ado_boards.backlog_counts(backlog)
    name = project.get("project_name") or "this project"
    if operation == "UPDATE_WORK_ITEMS_PROPOSAL":
        intro = f"Here is the updated, suggested **Azure Boards backlog** for **{name}**."
        if change_summary:
            intro += f" {str(change_summary).strip()}"
    else:
        intro = (f"Here is the suggested **Azure Boards backlog** for **{name}**, drafted from the PRD (version "
                 f"{project.get('prd_version') or 1}): 1 epic, {features} features and {stories} stories, one per "
                 "functional requirement, each with acceptance criteria.")
    parts = [intro, ado_boards.render_backlog(backlog, GENERIC_TYPES)]
    if project.get("work_items"):
        parts.append("Work items that already exist in Azure Boards will be updated and new ones added; nothing is "
                     "deleted.")
    parts.append("---")
    parts.append("Tell me what to change, or approve the backlog to create the work items in Azure Boards.")
    return "\n\n".join(parts)


async def create_work_items(project: Dict[str, Any]) -> str:
    draft = project.get("work_items_proposal")
    if not draft:
        return NO_DRAFT_MESSAGE
    backlog = {key: value for key, value in draft.items() if key != "synced"}
    progress.begin_side_effect("Creating the work items in Azure Boards")    # Stop can no longer cancel the turn
    result = await asyncio.to_thread(ado_boards.create_work_items, project["ado_project_id"], backlog,
                                     project.get("work_items"))
    status = result.get("status")
    fields: Dict[str, Any] = {"work_items": result.get("items") or project.get("work_items")}
    if status == "success":
        fields["work_items_prd_version"] = project.get("work_items_proposal_prd_version") or 0
        fields["work_items_proposal"] = dict(backlog, synced=True)
    store.update_project(project["project_id"], **fields)
    return render_create_reply(backlog, result)


def render_create_reply(backlog: Dict[str, Any], result: Dict[str, Any]) -> str:
    status = result.get("status")
    if status == "error":
        logger.error(f"[ADO Boards] {result.get('error_type')}: {result.get('message')} {result.get('details', '')}")
        return (f"The work items could not be created: {result.get('message')} Nothing was created in Azure Boards."
                "\n\n---\n\nFix the issue and ask me to create the work items again.")
    created, updated, unchanged = (len(result.get(k) or []) for k in ("created", "updated", "unchanged"))
    types = result.get("types") or {}
    names = {"epic": types.get("epic") or "Epic", "feature": types.get("feature") or "Group",
             "story": types.get("story") or "Story"}
    summary = f"{created} created" + (f", {updated} updated" if updated else "") + (
        f", {unchanged} unchanged" if unchanged else "")
    parts = []
    if status == "partial":
        error = result.get("error") or {}
        parts.append(f"Some work items could not be created ({summary} so far): {error.get('message')} Ask me to "
                     "create the work items again to continue; the ones already created will not be duplicated.")
    elif not created and not updated:
        parts.append(f"These work items already exist in Azure Boards and match the current draft, so nothing was "
                     f"created again (no duplicates). [Open the backlog]({result.get('backlog_url')})")
    else:
        parts.append(f"The work items are in Azure Boards ({summary}): one {names['epic']}, "
                     f"{names['feature']}s and one {names['story']} per requirement, with acceptance criteria. "
                     f"[Open the backlog]({result.get('backlog_url')})")
    parts.append(ado_boards.render_backlog(backlog, names, result.get("items")))
    if result.get("not_in_prd"):
        parts.append("Not in the PRD any more (kept, not deleted): " + ", ".join(result["not_in_prd"]) +
                     ". Remove them in Azure Boards if they are no longer needed.")
    parts.append("---")
    parts.append("When the PRD changes, ask me to update the work items and I'll sync them.")
    return "\n\n".join(parts)
