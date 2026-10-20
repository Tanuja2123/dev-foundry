"""
Azure Boards work items from the PRD (FEATURES.md ADO-1).

The backlog is drafted by the Backlog agent and reviewed by the user first; this module only validates it and creates
or updates the work items through the Azure DevOps REST API (no LLM involved).

Backlog shape (stored in Project.work_items_proposal):
    {"epic": {"key": "EPIC", "title", "description"},
     "features": [{"key": "F1", "title", "description",
                   "stories": [{"key": "FR-001", "title", "description", "acceptance_criteria": [str]}]}]}

Every PRD functional requirement becomes exactly one story whose key is the requirement id (FR-001...). The keys make
the sync idempotent: Project.work_items maps key -> {"id", "url", "type", "hash"}, so running it again after a PRD
change creates the new items, updates the changed ones and leaves unchanged ones alone. Nothing is ever deleted.

Work item types follow the project's process (read from the project's process configuration):
    Scrum: Epic > Feature > Product Backlog Item   Agile: Epic > Feature > User Story
    CMMI:  Epic > Feature > Requirement            Basic: Epic > Issue (no feature level; stories hang off the epic)
"""
import base64
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from app.core.logger import logger
from app.services.mcp.ado_mcp_server import get_ado_config, sanitize_error

API_VERSION = "7.1"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
ACCEPTANCE_FIELD = "Microsoft.VSTS.Common.AcceptanceCriteria"
PARENT_LINK = "System.LinkTypes.Hierarchy-Reverse"
TAG = "DevFoundry"

MAX_FEATURES = 30
MAX_STORIES = 150
MAX_TITLE = 255

# Tests replace these (httpx.MockTransport / no-op sleep).
transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None
_sleep = time.sleep


class BoardsError(Exception):
    def __init__(self, message: str, *, error_type: str = "BOARDS_FAILED", status_code: Optional[int] = None,
                 details: str = ""):
        super().__init__(message)
        self.message, self.error_type, self.status_code, self.details = message, error_type, status_code, details


# ---------------------------------------------------------------------------
# Backlog validation (the agent's draft -> a clean backlog that covers every PRD requirement)
# ---------------------------------------------------------------------------

_REQ_ID = re.compile(r"^[A-Z]{1,6}-\d{1,4}$")


def prd_requirements(prd: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """[(id, requirement text)] of the PRD's functional requirements, in order."""
    result = []
    for index, item in enumerate((prd or {}).get("Functional Requirements") or [], start=1):
        if isinstance(item, dict):
            req_id = str(item.get("id") or f"FR-{index:03d}").strip().upper()
            text = str(item.get("requirement") or item.get("description") or "").strip()
        else:
            req_id, text = f"FR-{index:03d}", str(item).strip()
        if text:
            result.append((req_id, text))
    return result


def _text(value: Any, limit: int = 4000) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()[:limit]


def _criteria(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [line.lstrip("-*• ").strip() for line in value.splitlines()]
    return [_text(c, 600) for c in (value or []) if _text(c)][:12]


TASK_LAYERS = ("Frontend", "Backend", "Database", "QA", "DevOps")
MAX_ACTION_WORDS = 10
_BRACKETS = re.compile(r"^\s*(\[[^\]]*\]\s*)+:?\s*")
_AS_A = re.compile(r"^\s*as an?\s+.+?,\s*i\s+want\s+(?:to\s+)?(.+?)(?:\s+so\s+that\b.*)?$", re.IGNORECASE)


def _words(text: str, limit: int) -> str:
    words = _text(text).rstrip(".").split()
    return " ".join(words[:limit])


def _action(title: Any, fallback: str) -> str:
    """The short action of a story title: no "[...]" prefixes, no "As a..., I want..." sentence, at most 10 words."""
    text = _BRACKETS.sub("", _text(title))
    match = _AS_A.match(text)
    if match:
        text = match.group(1)
    if not text:
        text = re.sub(r"^\s*the\s+system\s+shall\s+(?:let\s+|allow\s+)?", "", _text(fallback), flags=re.IGNORECASE)
    text = _words(text, MAX_ACTION_WORDS)
    return text[:1].upper() + text[1:] if text else "Requirement"


def _domain(value: Any, fallback: str) -> str:
    domain = re.sub(r"[^A-Za-z0-9 &/-]", "", _text(value or fallback))[:20].strip()
    return (domain[:1].upper() + domain[1:]) if domain else "General"


def _join_sections(*sections: Tuple[str, str]) -> str:
    """Description text: "**Label:** content" paragraphs; lists as "- " lines (rendered as HTML for Azure DevOps)."""
    parts = []
    for label, content in sections:
        if content:
            separator = "" if content.startswith("\n") else " "      # a list starts on the next line
            parts.append(f"**{label}:**{separator}{content}" if label else content)
    return "\n\n".join(parts)


def _bullets(items: List[str]) -> str:
    return "\n" + "\n".join(f"- {item}" for item in items) if items else ""


# A measurable non-functional goal (it needs technical work, not a feature): latency, availability, load, security...
_NON_FUNCTIONAL = re.compile(r"\b(p9\d|latency|response time|load time|page load|availability|uptime|"
                             r"concurren\w*|throughput|scal\w*|secur\w*|encrypt\w*|backup|recovery|ms\b|seconds?\b|minutes?\b|"
                             r"\d+(\.\d+)?\s*%\s*(uptime|availability)|99\.\d+\s*%)", re.IGNORECASE)


def normalize_backlog(draft: Any, prd: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Validates the agent's draft against the PRD and builds the board-ready backlog (FEATURES ADO-3). Returns
    (backlog, notes):
    - every PRD requirement is in exactly one story; a story groups related requirements, its key is its first
      requirement id (stable for the sync), unknown ids and repeats are dropped, missing ones get their own story
      under "Other requirements";
    - titles are built by code: Epic / Feature as written (no brackets), Story "[FR-002, FR-003] [Domain]: <action>"
      (action at most 10 words), Task "[Layer]: <action>";
    - descriptions are assembled from the agent's parts (user story, context, covered requirements with their PRD
      text, notes; epic goal / scope / success criteria; feature scope and out of scope).
    """
    requirements = prd_requirements(prd)
    known = {req_id: text for req_id, text in requirements}
    order = {req_id: n for n, (req_id, _) in enumerate(requirements)}
    notes: List[str] = []
    draft = draft if isinstance(draft, dict) else {}
    epic_in = draft.get("epic") if isinstance(draft.get("epic"), dict) else {}
    objective = _text((prd or {}).get("Objective"))
    goals = [_text(g.get("goal") if isinstance(g, dict) else g, 300) for g in (prd or {}).get("Goals") or []]
    epic = {"key": "EPIC",
            "title": _words(_BRACKETS.sub("", _text(epic_in.get("title"))), 12) or (objective[:120] or "Product backlog"),
            "description": _join_sections(
                ("Goal", _text(epic_in.get("goal") or epic_in.get("description")) or objective),
                ("Scope", _text(epic_in.get("scope"))),
                ("Success criteria", _bullets(_criteria(epic_in.get("success_criteria")) or [g for g in goals if g]))),
            "parts": {"goal": _text(epic_in.get("goal")), "scope": _text(epic_in.get("scope")),
                      "success_criteria": _criteria(epic_in.get("success_criteria"))}}

    features, used, seen_features = [], set(), set()
    for feature_in in draft.get("features") or []:
        if not isinstance(feature_in, dict) or len(features) >= MAX_FEATURES:
            continue
        key = str(feature_in.get("key") or "").strip().upper()
        if not re.match(r"^F\d{1,3}$", key) or key in seen_features:
            key = next(f"F{n}" for n in range(1, 1000) if f"F{n}" not in seen_features)
        feature_title = _words(_BRACKETS.sub("", _text(feature_in.get("title"))), 8) or f"Feature {key}"
        stories = []
        for story_in in feature_in.get("stories") or []:
            if not isinstance(story_in, dict):
                continue
            wanted = story_in.get("requirements") or [story_in.get("key") or story_in.get("id")]
            wanted = [str(r or "").strip().upper() for r in (wanted if isinstance(wanted, list) else [wanted])]
            unknown = [r for r in wanted if r and r not in known]
            if unknown:
                notes.append(f"dropped {', '.join(unknown)} (not PRD requirements)")
            reqs = sorted({r for r in wanted if r in known and r not in used}, key=order.get)
            if not reqs:
                continue
            used.update(reqs)
            domain = _domain(story_in.get("domain"), feature_title.split()[0] if feature_title else "")
            action = _action(story_in.get("title"), known[reqs[0]])
            story = {
                "key": reqs[0], "requirements": reqs, "domain": domain, "action": action,
                "title": f"[{', '.join(reqs)}] [{domain}]: {action}"[:MAX_TITLE],
                "description": _join_sections(
                    ("User story", _text(story_in.get("user_story"), 600)),
                    ("Context", _text(story_in.get("context") or story_in.get("description"), 1500)),
                    ("Requirements covered", _bullets([f"{r}: {known[r]}" for r in reqs])),
                    ("Notes", _text(story_in.get("notes"), 800))),
                "acceptance_criteria": _criteria(story_in.get("acceptance_criteria")),
                "tasks": [],
                # the agent's parts, so a later update can keep this story's details without rewriting them
                "parts": {"user_story": _text(story_in.get("user_story"), 600),
                          "context": _text(story_in.get("context") or story_in.get("description"), 1500),
                          "notes": _text(story_in.get("notes"), 800)},
            }
            for n, task_in in enumerate(story_in.get("tasks") or [], start=1):
                if not isinstance(task_in, dict) or n > 8:
                    continue
                layer = next((x for x in TASK_LAYERS if x.lower() == _text(task_in.get("layer")).lower()), "Backend")
                task_action = _words(_BRACKETS.sub("", _text(task_in.get("title"))), 12)
                if not task_action:
                    continue
                story["tasks"].append({
                    "key": f"{reqs[0]}-T{n}", "layer": layer, "title": f"[{layer}]: {task_action}"[:MAX_TITLE],
                    "description": _join_sections(("", _text(task_in.get("description"), 800)),
                                                  ("Done when", _text(task_in.get("done_when"), 300))),
                    "parts": {"layer": layer, "title": task_action, "description": _text(task_in.get("description"), 800),
                              "done_when": _text(task_in.get("done_when"), 300)}})
            stories.append(story)
        if not stories:
            continue
        seen_features.add(key)
        features.append({"key": key, "title": feature_title,
                         "description": _join_sections(
                             ("", _text(feature_in.get("description"), 1000)),
                             ("Out of scope", _bullets(_criteria(feature_in.get("out_of_scope"))))),
                         "stories": stories,
                         "parts": {"description": _text(feature_in.get("description"), 1000),
                                   "out_of_scope": _criteria(feature_in.get("out_of_scope"))}})

    # Technical enablers (FEATURES REV-9): stories for the non-functional goals, each linked to the goals it supports
    goals = [_text(g.get("goal") if isinstance(g, dict) else g, 400) for g in (prd or {}).get("Goals") or []]
    supported: set = set()
    enabler_stories = []
    for n, enabler in enumerate(draft.get("enablers") or [], start=1):
        if not isinstance(enabler, dict) or n > 8:
            continue
        action = _action(enabler.get("title"), "Technical enabler")
        numbers = [int(x) for x in enabler.get("goal_numbers") or [] if str(x).isdigit() and 1 <= int(x) <= len(goals)]
        supported.update(numbers)
        enabler_stories.append({
            "key": f"TE-{n}", "requirements": [], "domain": "Enabler", "action": action,
            "title": f"[TE-{n}] [Enabler]: {action}"[:MAX_TITLE],
            "description": _join_sections(
                ("", _text(enabler.get("description"), 1000)),
                ("Supports", _bullets([f"Goal {g}: {goals[g - 1]}" for g in numbers]))),
            "acceptance_criteria": _criteria(enabler.get("acceptance_criteria")), "tasks": [],
            "goal_numbers": numbers, "parts": {"description": _text(enabler.get("description"), 1000)}})

    missing = [(req_id, text) for req_id, text in requirements if req_id not in used]
    if missing:
        notes.append("added " + ", ".join(req_id for req_id, _ in missing) + " (missing from the draft)")
        key = next(f"F{n}" for n in range(1, 1000) if f"F{n}" not in seen_features)
        stories = []
        for req_id, text in missing:
            action = _action("", text)
            stories.append({"key": req_id, "requirements": [req_id], "domain": "General", "action": action,
                            "title": f"[{req_id}] [General]: {action}"[:MAX_TITLE],
                            "description": _join_sections(("Requirements covered", _bullets([f"{req_id}: {text}"]))),
                            "acceptance_criteria": [], "tasks": []})
        features.append({"key": key, "title": "Other requirements",
                         "description": "Requirements the draft did not place in a feature.", "stories": stories})
    if enabler_stories:
        key = next(f"F{n}" for n in range(1, 1000) if f"F{n}" not in {f["key"] for f in features})
        features.append({"key": key, "title": "Technical enablers",
                         "description": "Work the product needs that no single feature delivers: the non-functional "
                                        "goals and constraints of the PRD.", "stories": enabler_stories})
    if sum(len(f["stories"]) for f in features) > MAX_STORIES:
        raise BoardsError(f"The backlog has more than {MAX_STORIES} stories.", error_type="VALIDATION_ERROR")
    backlog = {"epic": epic, "features": features}
    uncovered = [f"Goal {n}: {g}" for n, g in enumerate(goals, start=1)
                 if n not in supported and _NON_FUNCTIONAL.search(g)]
    if uncovered:
        backlog["uncovered_goals"] = uncovered
    return backlog, notes


def backlog_counts(backlog: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    features = (backlog or {}).get("features") or []
    return len(features), sum(len(f.get("stories") or []) for f in features)


def flatten(backlog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Items in creation order (parents first): {"key", "level", "parent", "title", "description", "criteria", "tags"}."""
    items = [{"key": "EPIC", "level": "epic", "parent": None, "title": backlog["epic"]["title"],
              "description": backlog["epic"].get("description", ""), "criteria": [], "tags": []}]
    for feature in backlog["features"]:
        items.append({"key": feature["key"], "level": "feature", "parent": "EPIC", "title": feature["title"],
                      "description": feature.get("description", ""), "criteria": [], "tags": []})
        for story in feature["stories"]:
            items.append({"key": story["key"], "level": "story", "parent": feature["key"], "title": story["title"],
                          "description": story.get("description", ""),
                          "criteria": story.get("acceptance_criteria") or [],
                          "tags": list(story.get("requirements") or [story["key"]]) + (
                              [story["domain"]] if story.get("domain") else [])})
            for task in story.get("tasks") or []:
                items.append({"key": task["key"], "level": "task", "parent": story["key"], "title": task["title"],
                              "description": task.get("description", ""), "criteria": [],
                              "tags": [task["layer"]] if task.get("layer") else []})
    return items


# ---------------------------------------------------------------------------
# Rendering (the preview shown in chat)
# ---------------------------------------------------------------------------

def render_backlog(backlog: Dict[str, Any], types: Optional[Dict[str, str]] = None,
                   created: Optional[Dict[str, Any]] = None, criteria_for: Optional[set] = None) -> str:
    """The backlog as a nested list. criteria_for: show acceptance criteria only for these story keys (None = all)."""
    types = types or {"epic": "Epic", "feature": "Feature", "story": "Product Backlog Item"}
    created = created or {}

    def ref(key: str) -> str:
        item = created.get(key)
        return f" ([#{item['id']}]({item['url']}))" if item and item.get("id") and item.get("url") else ""

    lines = [f"**{types['epic']}:** {backlog['epic']['title']}{ref('EPIC')}", ""]
    for feature in backlog["features"]:
        lines.append(f"**{types.get('feature') or 'Group'}: {feature['title']}**{ref(feature['key'])}")
        for story in feature["stories"]:
            lines.append(f"- {story['title']}{ref(story['key'])}")
            if criteria_for is None or story["key"] in criteria_for:
                for criterion in story.get("acceptance_criteria") or []:
                    lines.append(f"  - {criterion}")
            for task in story.get("tasks") or []:
                lines.append(f"  - Task {task['title']}{ref(task['key'])}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Azure DevOps REST
# ---------------------------------------------------------------------------

@dataclass
class _Api:
    client: httpx.Client
    base: str            # https://dev.azure.com/{org}/{project}
    pat: str
    calls: int = field(default=0)

    def request(self, method: str, path: str, action: str, body: Any = None,
                content_type: str = "application/json") -> httpx.Response:
        url = f"{self.base}/_apis/{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            try:
                self.calls += 1
                response = self.client.request(method, url, content=None if body is None else json.dumps(body),
                                               headers={"Content-Type": content_type})
            except httpx.TransportError as exc:
                if last:
                    raise BoardsError(f"Network error while trying to {action}.", error_type="NETWORK_ERROR",
                                      details=sanitize_error(str(exc), self.pat)) from exc
                _sleep(2 ** attempt)
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and not last:
                _sleep(min(float(response.headers.get("Retry-After") or 2 ** attempt), 10.0))
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def json(self, response: httpx.Response, action: str, ok: Tuple[int, ...] = (200,)) -> Dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if response.status_code in ok and "json" in content_type:
            return response.json()
        status, body = response.status_code, sanitize_error(response.text, self.pat)[:500]
        if status == 401 or 300 <= status < 400 or (status < 300 and "json" not in content_type):
            raise BoardsError(f"Azure DevOps authentication failed while trying to {action}. The PAT is missing, "
                              "invalid or expired.", error_type="AUTHENTICATION_FAILED", status_code=status)
        if status == 403:
            raise BoardsError(f"Permission denied while trying to {action}. The PAT needs the 'Work Items (Read & "
                              "write)' scope.", error_type="PERMISSION_DENIED", status_code=status, details=body)
        if status == 404:
            raise BoardsError(f"Not found while trying to {action}.", error_type="NOT_FOUND", status_code=status,
                              details=body)
        raise BoardsError(f"Azure DevOps returned status {status} while trying to {action}.",
                          error_type="BAD_REQUEST" if status == 400 else "HTTP_ERROR", status_code=status,
                          details=body)


def detect_work_item_types(api: _Api) -> Dict[str, Any]:
    """The project's backlog levels: {"epic", "feature" (None for Basic), "story", "story_has_criteria"}."""
    config = api.json(api.request("GET", "work/processconfiguration", "read the project's process"),
                      "read the project's process")
    story = ((config.get("requirementBacklog") or {}).get("workItemTypes") or [{}])[0].get("name")
    portfolio = [t.get("name") for b in config.get("portfolioBacklogs") or [] for t in b.get("workItemTypes") or []]
    epic = "Epic" if "Epic" in portfolio else (portfolio[0] if portfolio else None)
    feature = "Feature" if "Feature" in portfolio else None
    if not story or not epic:
        raise BoardsError("The project's process has no backlog levels for work items.", error_type="UNSUPPORTED_PROCESS")
    fields = api.json(api.request("GET", f"wit/workitemtypes/{quote(story)}/fields", f"read the {story} fields"),
                      f"read the {story} fields")
    has_criteria = any(f.get("referenceName") == ACCEPTANCE_FIELD for f in fields.get("value") or [])
    return {"epic": epic, "feature": feature, "story": story, "task": "Task", "story_has_criteria": has_criteria}


def _html(text: str) -> str:
    """Description text -> HTML for Azure DevOps: "**Label:**" in bold, "- " lines as lists, paragraphs kept."""
    blocks = []
    for paragraph in (text or "").split("\n\n"):
        lines, out, bullets = paragraph.split("\n"), [], []
        for line in lines:
            if line.startswith("- "):
                bullets.append(f"<li>{html.escape(line[2:])}</li>")
                continue
            if bullets:
                out.append("<ul>" + "".join(bullets) + "</ul>")
                bullets = []
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(line))
            if escaped:
                out.append(escaped)
        if bullets:
            out.append("<ul>" + "".join(bullets) + "</ul>")
        if out:
            blocks.append("<br>".join(out).replace("<br><ul>", "<ul>").replace("</ul><br>", "</ul>"))
    return "".join(f"<div>{block}</div>" for block in blocks)


def _criteria_html(criteria: List[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in criteria) + "</ul>" if criteria else ""


def _content_hash(item: Dict[str, Any], work_item_type: str) -> str:
    payload = [work_item_type, item["title"], item["description"], item["criteria"], item["parent"],
               item.get("tags") or []]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def _fields_patch(item: Dict[str, Any], types: Dict[str, Any], op: str = "add") -> List[Dict[str, Any]]:
    description = _html(item["description"])
    patch = [{"op": op, "path": "/fields/System.Title", "value": item["title"][:MAX_TITLE]}]
    if item["level"] == "story" and item["criteria"]:
        if types["story_has_criteria"]:
            patch.append({"op": op, "path": f"/fields/{ACCEPTANCE_FIELD}", "value": _criteria_html(item["criteria"])})
        else:
            description += "<div><b>Acceptance criteria</b></div>" + _criteria_html(item["criteria"])
    patch.append({"op": op, "path": "/fields/System.Description", "value": description})
    # Tags: DevFoundry + the requirement ids and domain of a story (managers filter by them) / the layer of a task
    tags = "; ".join([TAG] + [t for t in item.get("tags") or [] if t])
    patch.append({"op": op, "path": "/fields/System.Tags", "value": tags})
    return patch


def _parent_relation(api: _Api, parent_id: int) -> Dict[str, Any]:
    return {"op": "add", "path": "/relations/-",
            "value": {"rel": PARENT_LINK, "url": f"{api.base.rsplit('/', 1)[0]}/_apis/wit/workItems/{parent_id}"}}


def _work_item_type(level: str, types: Dict[str, Any]) -> str:
    if level == "task":
        return types.get("task") or "Task"
    return types["epic"] if level == "epic" else (types["feature"] if level == "feature" else types["story"])


def sync_work_items(project: str, backlog: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Creates / updates the backlog's work items in the Azure DevOps project (name or id). Idempotent: items already in
    `existing` are only updated when their content changed. On an error part-way, the items created so far are
    returned in `items` together with `error`, so a retry continues where it stopped.
    Returns {"items", "created", "updated", "unchanged", "not_in_prd", "types", "backlog_url", "error"?}.
    """
    config = get_ado_config()
    if not config.get("org_url") or not config.get("pat"):
        raise BoardsError("Azure DevOps configuration missing (AZURE_DEVOPS_ORG_NAME / AZURE_DEVOPS_PAT).",
                          error_type="CONFIG_MISSING")
    pat = config["pat"]
    headers = {"Authorization": "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii"),
               "Accept": "application/json"}
    items: Dict[str, Any] = dict(existing or {})
    result: Dict[str, Any] = {"items": items, "created": [], "updated": [], "unchanged": [], "not_in_prd": []}
    base = f"{config['org_url']}/{quote(project)}"
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers, transport=transport_factory()) as client:
        api = _Api(client=client, base=base, pat=pat)
        types = detect_work_item_types(api)
        result["types"] = types
        result["backlog_url"] = f"{base}/_backlogs/backlog"
        flat = flatten(backlog)
        if not types["feature"]:          # Basic process: no feature level, stories hang off the epic
            flat = [dict(i, parent="EPIC") for i in flat if i["level"] != "feature"]
        try:
            for item in flat:
                _sync_item(api, item, types, items, result)
        except BoardsError as exc:
            result["error"] = {"message": exc.message, "error_type": exc.error_type, "details": exc.details}
            logger.error(f"[ADO Boards] Stopped after {len(result['created'])} created / {len(result['updated'])} "
                         f"updated: {exc.message} {exc.details}")
        keys = {i["key"] for i in flat}
        result["not_in_prd"] = sorted(k for k in items if k not in keys)
        logger.info(f"[ADO Boards] {project}: {len(result['created'])} created, {len(result['updated'])} updated, "
                    f"{len(result['unchanged'])} unchanged ({api.calls} requests)")
    return result


def _sync_item(api: _Api, item: Dict[str, Any], types: Dict[str, Any], items: Dict[str, Any],
               result: Dict[str, Any]) -> None:
    work_item_type = _work_item_type(item["level"], types)
    content_hash = _content_hash(item, work_item_type)
    known = items.get(item["key"])
    parent_id = (items.get(item["parent"]) or {}).get("id") if item["parent"] else None

    if known and known.get("id"):
        if known.get("hash") == content_hash:
            result["unchanged"].append(item["key"])
            return
        current = api.request("GET", f"wit/workitems/{known['id']}?$expand=relations", f"read work item {known['id']}")
        if current.status_code != 404:       # deleted in Azure Boards -> created again below
            data = api.json(current, f"read work item {known['id']}")
            patch = _fields_patch(item, types)
            if parent_id:
                relations = data.get("relations") or []
                index = next((n for n, r in enumerate(relations) if r.get("rel") == PARENT_LINK), None)
                current_parent = relations[index]["url"].rsplit("/", 1)[-1] if index is not None else None
                if current_parent != str(parent_id):
                    if index is not None:
                        patch.append({"op": "remove", "path": f"/relations/{index}"})
                    patch.append(_parent_relation(api, parent_id))
            api.json(api.request("PATCH", f"wit/workitems/{known['id']}", f"update work item {known['id']}", patch,
                                 "application/json-patch+json"), f"update work item {known['id']}")
            items[item["key"]] = dict(known, type=work_item_type, hash=content_hash)
            result["updated"].append(item["key"])
            return

    patch = _fields_patch(item, types)
    if parent_id:
        patch.append(_parent_relation(api, parent_id))
    action = f"create the {work_item_type} '{item['title'][:60]}'"
    data = api.json(api.request("POST", f"wit/workitems/${quote(work_item_type)}", action, patch,
                                "application/json-patch+json"), action)
    url = ((data.get("_links") or {}).get("html") or {}).get("href") or data.get("url")
    items[item["key"]] = {"id": data["id"], "url": url, "type": work_item_type, "hash": content_hash}
    result["created"].append(item["key"])


def create_work_items(project: str, backlog: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Tool-style wrapper: never raises; returns {"status": "success"|"partial"|"error", ...}."""
    try:
        result = sync_work_items(project, backlog, existing)
    except BoardsError as exc:
        return {"status": "error", "error_type": exc.error_type, "message": exc.message, "details": exc.details,
                "items": dict(existing or {})}
    result["status"] = "partial" if result.get("error") else "success"
    return result
