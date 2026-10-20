"""
Azure Boards work items from the PRD (FEATURES.md ADO-1): the three Backlog_Agent operations.

GENERATE_WORK_ITEMS_PROPOSAL / UPDATE_WORK_ITEMS_PROPOSAL: the Backlog agent drafts / changes the backlog in two small
steps (ISSUES AGT-48): the planner returns the structure, then the writer fills in the details one feature at a time
(in parallel); stories a change doesn't touch keep their details. The agent gets the PRD, the current draft's outline
and the user's message, not the whole conversation. Code validates the result against the PRD (every requirement
exactly once), stores it and renders the preview (no synthesizer, so the preview always matches what will be created).
CREATE_WORK_ITEMS: code only. Creates / updates the work items in the project's Azure Boards (idempotent by key).
"""
import asyncio
import json
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent_framework import Message

from app.core.logger import logger
from app.services import ado_boards
from app.services import progress
from app.services import project_store as store
from app.services.agents.backlog_agent import get_backlog_planner, get_backlog_writer

MIGRATION_006_MESSAGE = ("Work items need a one-time database update first: run "
                         "`backend/db/migrations/006_work_items.sql` in the Supabase SQL editor, then ask again.")
NO_ADO_PROJECT_MESSAGE = ("Work items are created in the project's Azure Boards, and the Azure DevOps project doesn't "
                          "exist yet. Create the project first, then ask me to plan the work items.")
NO_DRAFT_MESSAGE = "There is no work items draft yet. Ask me to plan the work items from the PRD first."
DRAFT_FAILED_MESSAGE = ("I couldn't draft the work items this time because the AI service is busy or returned an "
                        "unusable answer, so nothing was changed. (Reference: {reference})\n\n---\n\n"
                        "Please ask again in a minute.")
GENERIC_TYPES = {"epic": "Epic", "feature": "Feature", "story": "Story"}
REPAIR_REQUEST = ("Your previous answer is not the required JSON object. Return the same answer again as ONE valid "
                  "JSON object following the OUTPUT shape, nothing else.")


def _parse(text: str) -> Optional[Dict[str, Any]]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _ask(agent: Any, payload: Dict[str, Any], label: str, valid: Callable[[Dict[str, Any]], bool],
               reference: str) -> Optional[Dict[str, Any]]:
    """One agent call with one repair attempt. None when no usable JSON came back (logged under the reference)."""
    messages = [Message(role="user", contents=[json.dumps(payload, ensure_ascii=False)])]
    for attempt in range(2):
        try:
            response = await agent.run(messages=messages)
        except Exception as exc:                 # every provider failed: the chain already tried them all
            logger.error(f"[Backlog_Agent] [ref {reference}] {label}: {type(exc).__name__}: {exc}")
            return None
        raw = response.messages[-1].text if response.messages else ""
        data = _parse(raw)
        if data is not None and valid(data):
            return data
        logger.warning(f"[Backlog_Agent] [ref {reference}] {label}: output is not the expected JSON"
                       + ("; asking again" if attempt == 0 else ""))
        messages = messages + [Message(role="assistant", contents=[raw or "(empty)"]),
                               Message(role="user", contents=[REPAIR_REQUEST])]
    return None


async def run_backlog_operation(client: Any, operation: str, user_query: str, message_list: List[Any],
                                state: Dict[str, Any]) -> str:
    project = state.get("project") or {}
    if not store.has_work_item_columns(project):
        return MIGRATION_006_MESSAGE
    if not project.get("ado_project_id"):
        return NO_ADO_PROJECT_MESSAGE
    if operation == "CREATE_WORK_ITEMS":
        return await create_work_items(project)
    return await draft_work_items(client, operation, user_query, project)


# ---------------------------------------------------------------------------- inputs for the agent

def _prd_context(prd: Dict[str, Any]) -> Dict[str, Any]:
    goals = [g.get("goal") if isinstance(g, dict) else g for g in prd.get("Goals") or []]
    return {"objective": prd.get("Objective"), "target_users": prd.get("Target Users"),
            "goals": [f"{n}. {g}" for n, g in enumerate(goals, start=1)], "non_goals": prd.get("Non-Goals"),
            "constraints": prd.get("Constraints"), "assumptions": prd.get("Assumptions")}


def _is_enabler(story: Dict[str, Any]) -> bool:
    return str(story.get("key") or "").startswith("TE-")


def _outline(backlog: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The current draft without its long texts: what the planner needs to keep or change the structure."""
    if not backlog:
        return None
    features, enablers = [], []
    for feature in backlog.get("features") or []:
        stories = feature.get("stories") or []
        if stories and all(_is_enabler(s) for s in stories):
            enablers += [{"key": s["key"], "title": s.get("action") or s.get("title"),
                          "goal_numbers": s.get("goal_numbers") or []} for s in stories]
            continue
        features.append({"key": feature.get("key"), "title": feature.get("title"),
                         "description": (feature.get("parts") or {}).get("description") or feature.get("description"),
                         "stories": [{"key": s["key"], "requirements": s.get("requirements") or [s["key"]],
                                      "domain": s.get("domain"), "title": s.get("action") or s.get("title"),
                                      "tasks": [t.get("title") for t in s.get("tasks") or []]} for s in stories]})
    epic = backlog.get("epic") or {}
    return {"epic": {"title": epic.get("title"), **(epic.get("parts") or {})}, "features": features,
            "enablers": enablers}


def _existing_stories(backlog: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """(feature stories by key, enablers by lower-case title) of the current draft."""
    stories, enablers = {}, {}
    for feature in (backlog or {}).get("features") or []:
        for story in feature.get("stories") or []:
            if _is_enabler(story):
                enablers[str(story.get("action") or "").lower()] = story
            else:
                stories[story["key"]] = story
    return stories, enablers


def _current_details(story: Dict[str, Any]) -> Dict[str, Any]:
    parts = story.get("parts") or {}
    return {"user_story": parts.get("user_story", ""), "context": parts.get("context", ""),
            "notes": parts.get("notes", ""), "description": parts.get("description", ""),
            "acceptance_criteria": story.get("acceptance_criteria") or [],
            "tasks": [t.get("parts") or {"layer": t.get("layer"), "title": t.get("title")}
                      for t in story.get("tasks") or []]}


# ---------------------------------------------------------------------------- the two steps

def _valid_plan(data: Dict[str, Any]) -> bool:
    features = data.get("features")
    return isinstance(features, list) and any(isinstance(f, dict) and f.get("stories") for f in features)


MAX_PLANNED_STORIES = 8


def _story_count(plan: Dict[str, Any]) -> int:
    return sum(len(f.get("stories") or []) for f in plan.get("features") or [] if isinstance(f, dict))


def _missing_requirements(plan: Dict[str, Any], known: Dict[str, str]) -> List[str]:
    placed = {str(r).upper() for f in plan.get("features") or [] if isinstance(f, dict)
              for s in f.get("stories") or [] if isinstance(s, dict) for r in s.get("requirements") or []}
    return [r for r in known if r not in placed]


def _valid_details(data: Dict[str, Any]) -> bool:
    return isinstance(data.get("stories"), list)


async def draft_work_items(client: Any, operation: str, user_query: str, project: Dict[str, Any]) -> str:
    prd = project.get("prd") or {}
    current = project.get("work_items_proposal") or None
    if operation == "UPDATE_WORK_ITEMS_PROPOSAL" and not current:
        operation = "GENERATE_WORK_ITEMS_PROPOSAL"
    reference = uuid.uuid4().hex[:8]
    requirements = ado_boards.prd_requirements(prd)
    known = dict(requirements)
    order = {req_id: n for n, (req_id, _) in enumerate(requirements)}
    logger.info(f"[Backlog_Agent] {operation}: {len(requirements)} PRD requirements (planner + writer per feature)")

    # 1. Structure
    progress.detail("Planning the features and stories")
    plan_request = {
        "operation": operation, "user_message": user_query, "prd_context": _prd_context(prd),
        "requirements": [{"id": r, "text": t} for r, t in requirements], "current_outline": _outline(current)}
    plan = await _ask(get_backlog_planner(client), plan_request, "plan", _valid_plan, reference)
    if plan is None:
        return DRAFT_FAILED_MESSAGE.format(reference=reference)
    missing = _missing_requirements(plan, known)
    too_many = operation == "GENERATE_WORK_ITEMS_PROPOSAL" and _story_count(plan) > MAX_PLANNED_STORIES
    if missing or too_many:
        # seen live: the planner left out FR-012..FR-015 and FR-017 (auth, data), or made 14 stories for 16
        # requirements (decided: about 4-8 stories that group related requirements); one correction, naming the issue
        problems = ([f"it leaves out {', '.join(missing)}"] if missing else []) + (
            [f"it has {_story_count(plan)} stories; group related requirements into at most {MAX_PLANNED_STORIES} "
             "(2-4 requirements per story)"] if too_many else [])
        logger.warning(f"[Backlog_Agent] [ref {reference}] plan: {'; '.join(problems)}; asking the planner again")
        retry = await _ask(get_backlog_planner(client), {
            **plan_request, "previous_plan": plan,
            "correction": "Your plan needs a fix: " + "; ".join(problems) + ". Return the complete plan again with "
                          "EVERY requirement in exactly one story."},
            "plan (correction)", _valid_plan, reference)
        if retry is not None and len(_missing_requirements(retry, known)) <= len(missing) and (
                not too_many or _story_count(retry) < _story_count(plan)):
            plan = retry
        missing = _missing_requirements(plan, known)
        if missing:                        # still missing: their own stories, written in full by the writer
            plan.setdefault("features", []).append({
                "key": None, "title": "Other requirements", "description": "Requirements not placed in a feature.",
                "out_of_scope": [], "stories": [{"requirements": [r], "domain": "General", "title": known[r]}
                                                 for r in missing]})

    tasks_mode = str(plan.get("tasks") or "none").lower()
    had_tasks = any(s.get("tasks") for f in (current or {}).get("features") or [] for s in f.get("stories") or [])
    if operation == "UPDATE_WORK_ITEMS_PROPOSAL" and had_tasks and tasks_mode == "none":
        tasks_mode = "add"                      # keep the existing tasks unless the user asked to remove them
    rewrite = {str(k).upper() for k in plan.get("rewrite") or []}
    rewrite_all = operation == "GENERATE_WORK_ITEMS_PROPOSAL" or "ALL" in rewrite
    instruction = str(plan.get("instruction") or "") if operation == "UPDATE_WORK_ITEMS_PROPOSAL" else ""
    old_stories, old_enablers = _existing_stories(current)

    # 2. Which stories keep their details and which get them written (per feature)
    details: Dict[str, Dict[str, Any]] = {}
    jobs: List[Tuple[str, Dict[str, Any]]] = []
    for feature in plan.get("features") or []:
        if not isinstance(feature, dict):
            continue
        todo = []
        for story in feature.get("stories") or []:
            if not isinstance(story, dict):
                continue
            reqs = sorted({str(r).upper() for r in story.get("requirements") or [] if str(r).upper() in known},
                          key=order.get)
            if not reqs:
                continue
            key, old = reqs[0], old_stories.get(reqs[0])
            same = bool(old) and set(old.get("requirements") or [old["key"]]) == set(reqs) and old.get("parts")
            needs_tasks = tasks_mode == "add" and same and not old.get("tasks")
            if same and not rewrite_all and key not in rewrite and not needs_tasks:
                details[key] = _current_details(old)
                continue
            todo.append({"key": key, "title": story.get("title"), "domain": story.get("domain"),
                         "requirements": [{"id": r, "text": known[r]} for r in reqs],
                         **({"current": _current_details(old)} if old and not rewrite_all else {})})
        if todo:
            jobs.append((f"feature {feature.get('key') or feature.get('title')}", {
                "feature": {"title": feature.get("title"), "description": feature.get("description")},
                "stories": todo, "tasks": "add" if tasks_mode == "add" else "none"}))
    goals = _prd_context(prd)["goals"]
    enablers_todo = []
    for n, enabler in enumerate(plan.get("enablers") or [], start=1):
        if not isinstance(enabler, dict):
            continue
        old = old_enablers.get(str(enabler.get("title") or "").lower())
        if old and old.get("parts") and not rewrite_all and f"TE-{n}" not in rewrite:
            details[f"TE-{n}"] = _current_details(old)
            continue
        numbers = [int(x) for x in enabler.get("goal_numbers") or [] if str(x).isdigit()]
        enablers_todo.append({"key": f"TE-{n}", "title": enabler.get("title"),
                              "supports_goals": [goals[g - 1] for g in numbers if 1 <= g <= len(goals)]})
    if enablers_todo:
        jobs.append(("enablers", {"feature": {"title": "Technical enablers"}, "stories": enablers_todo,
                                  "tasks": "none"}))

    if jobs:
        progress.detail(f"Writing the details ({len(jobs)} part{'s' if len(jobs) != 1 else ''} in parallel)")
        writer = get_backlog_writer(client)
        context = _prd_context(prd)
        results = await asyncio.gather(*[
            _ask(writer, {**payload, "prd_context": context, "instruction": instruction}, label, _valid_details,
                 reference) for label, payload in jobs])
        if any(result is None for result in results):
            return DRAFT_FAILED_MESSAGE.format(reference=reference)
        for result in results:
            for story in result.get("stories") or []:
                if isinstance(story, dict) and story.get("key"):
                    details[str(story["key"]).upper()] = story

    # 3. Assemble, validate against the PRD, store
    draft = _assemble(plan, details, known, order, tasks_mode, current)
    backlog, notes = ado_boards.normalize_backlog(draft, prd)
    if notes:
        logger.warning(f"[Backlog_Agent] Draft corrected: {'; '.join(notes)}")
    prd_version = project.get("prd_version") or 0
    store.update_project(project["project_id"], work_items_proposal=backlog,
                         work_items_proposal_prd_version=prd_version)
    features, stories = ado_boards.backlog_counts(backlog)
    logger.info(f"[Backlog_Agent] Draft saved: {features} features, {stories} stories, {len(jobs)} writer call(s) "
                f"(PRD v{prd_version})")
    changed = None if operation == "GENERATE_WORK_ITEMS_PROPOSAL" else _changed_criteria(current, backlog)
    return render_draft_reply(project, backlog, operation, plan.get("change_summary"), changed)


def _assemble(plan: Dict[str, Any], details: Dict[str, Dict[str, Any]], known: Dict[str, str],
              order: Dict[str, int], tasks_mode: str, current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The plan plus the details in the agent-draft shape that ado_boards.normalize_backlog validates."""
    def story_details(key: str) -> Dict[str, Any]:
        found = dict(details.get(key) or {})
        if tasks_mode in ("none", "remove"):
            found["tasks"] = []
        return found

    epic = dict(plan.get("epic") or {})
    if current and not (epic.get("goal") or epic.get("scope")):      # an update that left the epic alone
        epic = {**(current.get("epic") or {}).get("parts", {}), "title": epic.get("title") or current["epic"]["title"]}
    features = []
    for feature in plan.get("features") or []:
        if not isinstance(feature, dict):
            continue
        stories = []
        for story in feature.get("stories") or []:
            if not isinstance(story, dict):
                continue
            reqs = sorted({str(r).upper() for r in story.get("requirements") or [] if str(r).upper() in known},
                          key=order.get)
            if reqs:
                stories.append({**story_details(reqs[0]), "requirements": reqs, "domain": story.get("domain"),
                                "title": story.get("title")})
        features.append({"key": feature.get("key"), "title": feature.get("title"),
                         "description": feature.get("description"), "out_of_scope": feature.get("out_of_scope"),
                         "stories": stories})
    enablers = []
    for n, enabler in enumerate(plan.get("enablers") or [], start=1):
        if isinstance(enabler, dict):
            found = details.get(f"TE-{n}") or {}
            enablers.append({"title": enabler.get("title"), "goal_numbers": enabler.get("goal_numbers") or [],
                             "description": found.get("description") or found.get("context") or "",
                             "acceptance_criteria": found.get("acceptance_criteria") or []})
    return {"epic": epic, "features": features, "enablers": enablers}


def _changed_criteria(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> Set[str]:
    """Keys of the stories whose acceptance criteria are new or changed (shown in an update's preview)."""
    old = {s["key"]: s.get("acceptance_criteria") for f in (before or {}).get("features") or [] for s in f["stories"]}
    return {s["key"] for f in after["features"] for s in f["stories"] if old.get(s["key"]) != s.get("acceptance_criteria")}


def render_draft_reply(project: Dict[str, Any], backlog: Dict[str, Any], operation: str,
                       change_summary: Optional[str] = None, changed: Optional[Set[str]] = None) -> str:
    features, stories = ado_boards.backlog_counts(backlog)
    name = project.get("project_name") or "this project"
    tasks = sum(len(s.get("tasks") or []) for f in backlog.get("features") or [] for s in f.get("stories") or [])
    if operation == "UPDATE_WORK_ITEMS_PROPOSAL":
        intro = f"Here is the updated, suggested **Azure Boards backlog** for **{name}**."
        if change_summary:
            intro += f" {str(change_summary).strip()}"
        # Only what changed in detail (ISSUES AGT-50): acceptance criteria of the stories the change touched
        intro += (" Acceptance criteria are shown for the stories whose criteria changed; the others are unchanged."
                  if changed is not None and len(changed) < stories else "")
    else:
        requirements = len(ado_boards.prd_requirements(project.get("prd")))
        intro = (f"Here is the suggested **Azure Boards backlog** for **{name}**, drafted from the PRD (version "
                 f"{project.get('prd_version') or 1}): 1 epic, {features} features and {stories} stories covering "
                 f"all {requirements} requirements, each with acceptance criteria"
                 + (f", and {tasks} tasks" if tasks else "") + ". Each item has a full description in Azure Boards.")
    parts = [intro, ado_boards.render_backlog(backlog, GENERIC_TYPES, criteria_for=changed)]
    if backlog.get("uncovered_goals"):
        parts.append("Not covered by any work item yet: " + "; ".join(backlog["uncovered_goals"]) + ". Say \"add an "
                     "enabler for it\", or leave it out on purpose.")
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
    backlog = {key: value for key, value in draft.items() if key not in ("synced", "uncovered_goals")}
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
        # Nothing new: a short confirmation, never the whole list again (ISSUES AGT-30)
        return (f"All {unchanged} work items already exist in Azure Boards and match the current draft, so nothing "
                f"was created again (no duplicates). [Open the backlog]({result.get('backlog_url')})\n\n---\n\n"
                "When the PRD changes, ask me to update the work items and I'll sync them.")
    else:
        parts.append(f"The work items are in Azure Boards ({summary}): one {names['epic']}, a few "
                     f"{names['feature']}s and {names['story']}s that cover every requirement, with acceptance "
                     f"criteria. [Open the backlog]({result.get('backlog_url')})")
    parts.append(ado_boards.render_backlog(backlog, names, result.get("items")))
    if result.get("not_in_prd"):
        parts.append("No longer in the backlog (kept in Azure Boards, not deleted): " + ", ".join(result["not_in_prd"])
                     + ". Remove them there if they are no longer needed.")
    parts.append("---")
    parts.append("When the PRD changes, ask me to update the work items and I'll sync them.")
    return "\n\n".join(parts)
