"""Azure Boards work items from the PRD (ADO-1): validation, sync against a mocked Azure DevOps API, agent flow, routing."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import ado_boards, backlog_service
from app.services import project_store as store
from app.services import workflow_routing as wr
from tests.fake_supabase import FakeSupabase

PRD = {"Objective": "Let 2,000 patients book clinic appointments online.",
       "Target Users": ["Patient", "Receptionist"],
       "Functional Requirements": [
           {"id": "FR-001", "requirement": "The system shall let patients book an appointment."},
           {"id": "FR-002", "requirement": "The system shall email a reminder 24 hours before."},
           {"id": "FR-003", "requirement": "The system shall let receptionists manage doctors' schedules."}]}

DRAFT = {"epic": {"title": "Online clinic booking", "description": "Patients book online."},
         "features": [
             {"key": "F1", "title": "Booking", "description": "Book appointments.", "stories": [
                 {"key": "FR-001", "title": "As a patient, I want to book so that I save time",
                  "description": "Booking.", "acceptance_criteria": ["Given a free slot, when I book, then it is mine"]},
                 {"key": "FR-002", "title": "As a patient, I want a reminder so that I don't forget",
                  "description": "Reminder.", "acceptance_criteria": ["A reminder is sent 24 hours before"]}]},
             {"key": "F2", "title": "Schedules", "description": "Manage schedules.", "stories": [
                 {"key": "FR-003", "title": "As a receptionist, I want to manage schedules so that slots are right",
                  "description": "Schedules.", "acceptance_criteria": ["Changes appear immediately"]}]}]}


# ---------------------------------------------------------------------------- validation and rendering

def test_normalize_keeps_a_valid_draft():
    backlog, notes = ado_boards.normalize_backlog(DRAFT, PRD)
    assert notes == []
    assert [f["key"] for f in backlog["features"]] == ["F1", "F2"]
    assert [s["key"] for f in backlog["features"] for s in f["stories"]] == ["FR-001", "FR-002", "FR-003"]


def test_normalize_covers_every_requirement_exactly_once():
    draft = {"epic": {}, "features": [
        {"key": "F1", "title": "Booking", "stories": [
            {"key": "FR-001", "title": "Book"}, {"key": "FR-001", "title": "Duplicate"},
            {"key": "FR-099", "title": "Invented"}]},
        {"key": "F1", "title": "Same key", "stories": [{"key": "fr-002", "title": "Remind"}]},
        {"key": "F3", "title": "Empty", "stories": []}]}
    backlog, notes = ado_boards.normalize_backlog(draft, PRD)
    stories = [s["key"] for f in backlog["features"] for s in f["stories"]]
    assert stories == ["FR-001", "FR-002", "FR-003"]                       # invented dropped, missing added
    assert [f["key"] for f in backlog["features"]] == ["F1", "F2", "F3"]   # unique keys, empty feature removed
    assert backlog["features"][-1]["title"] == "Other requirements"
    assert backlog["epic"]["title"].startswith("Let 2,000 patients")       # epic falls back to the objective
    assert any("FR-099" in n for n in notes) and any("FR-003" in n for n in notes)


def test_render_shows_the_hierarchy_and_created_links():
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    text = ado_boards.render_backlog(backlog, created={"FR-001": {"id": 7, "url": "https://dev.azure.com/o/p/_workitems/edit/7"}})
    assert "**Epic:** Online clinic booking" in text
    assert "**Feature: Booking**" in text
    # ADO-3: short board titles built by code; the "As a…" sentence moved to the description
    assert "- [FR-001] [Booking]: Book so that I save time ([#7](https://dev.azure.com/o/p/_workitems/edit/7))" in text \
        or "- [FR-001] [Booking]: Book ([#7](https://dev.azure.com/o/p/_workitems/edit/7))" in text
    assert "  - A reminder is sent 24 hours before" in text


# ---------------------------------------------------------------------------- Azure DevOps (mocked)

class FakeBoards:
    """Minimal Azure Boards REST API: process configuration, fields, create, read, update."""

    def __init__(self, process="Scrum", fail_on=None, status=None):
        self.process, self.fail_on, self.status = process, fail_on, status
        self.items, self.requests, self.next_id = {}, [], 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        if self.status:
            return httpx.Response(self.status, json={"message": "denied"})
        if path.endswith("/work/processconfiguration"):
            story = {"Scrum": "Product Backlog Item", "Agile": "User Story", "Basic": "Issue"}[self.process]
            portfolio = [{"name": "Epics", "workItemTypes": [{"name": "Epic"}]}]
            if self.process != "Basic":
                portfolio.append({"name": "Features", "workItemTypes": [{"name": "Feature"}]})
            return httpx.Response(200, json={"requirementBacklog": {"workItemTypes": [{"name": story}]},
                                             "portfolioBacklogs": portfolio})
        if "/fields" in path:
            fields = [{"referenceName": "System.Title"}]
            if self.process != "Basic":
                fields.append({"referenceName": ado_boards.ACCEPTANCE_FIELD})
            return httpx.Response(200, json={"value": fields})
        if method == "POST" and "/wit/workitems/$" in path:
            if self.fail_on and any(op.get("value") == self.fail_on for op in body):
                return httpx.Response(400, json={"message": "bad field"})
            self.next_id += 1
            self.items[self.next_id] = {"type": path.rsplit("$", 1)[1], "patch": body,
                                        "relations": [op["value"] for op in body if op["path"] == "/relations/-"]}
            return httpx.Response(200, json={"id": self.next_id, "_links": {"html": {
                "href": f"https://dev.azure.com/org/p/_workitems/edit/{self.next_id}"}}})
        item_id = int(path.rstrip("/").rsplit("/", 1)[1])
        if item_id not in self.items:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(200, json={"id": item_id, "relations": self.items[item_id]["relations"]})
        self.items[item_id]["patch"] = body
        for op in body:
            if op["op"] == "remove":
                self.items[item_id]["relations"].pop(int(op["path"].rsplit("/", 1)[1]))
            elif op["path"] == "/relations/-":
                self.items[item_id]["relations"].append(op["value"])
        return httpx.Response(200, json={"id": item_id})


@pytest.fixture
def boards(monkeypatch):
    fake = FakeBoards()
    monkeypatch.setattr(ado_boards, "transport_factory", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(ado_boards, "get_ado_config", lambda: {"org_url": "https://dev.azure.com/org", "pat": "secret"})
    monkeypatch.setattr(ado_boards, "_sleep", lambda s: None)
    return fake


def _field(patch, name):
    return next((op["value"] for op in patch if op["path"] == f"/fields/{name}"), None)


def test_creates_epic_features_and_stories_with_parents(boards):
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    result = ado_boards.create_work_items("proj-id", backlog)
    assert result["status"] == "success"
    assert result["created"] == ["EPIC", "F1", "FR-001", "FR-002", "F2", "FR-003"]
    assert [boards.items[result["items"][k]["id"]]["type"] for k in ("EPIC", "F1", "FR-001")] == \
        ["Epic", "Feature", "Product Backlog Item"]
    story = boards.items[result["items"]["FR-001"]["id"]]
    assert _field(story["patch"], "System.Tags") == "DevFoundry; FR-001; Booking"      # requirement id + domain
    assert "<li>Given a free slot, when I book, then it is mine</li>" in _field(story["patch"], ado_boards.ACCEPTANCE_FIELD)
    parent_id = result["items"]["F1"]["id"]
    assert story["relations"][0]["rel"] == ado_boards.PARENT_LINK
    assert story["relations"][0]["url"].endswith(f"/_apis/wit/workItems/{parent_id}")
    assert result["backlog_url"] == "https://dev.azure.com/org/proj-id/_backlogs/backlog"
    assert "secret" not in json.dumps(result)


def test_sync_is_idempotent_and_updates_only_changes(boards):
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    first = ado_boards.create_work_items("p", backlog)
    again = ado_boards.create_work_items("p", backlog, first["items"])
    assert again["created"] == [] and again["updated"] == [] and len(again["unchanged"]) == 6

    changed = json.loads(json.dumps(DRAFT))
    changed["features"][0]["stories"][0]["title"] = "As a patient, I want to book in two clicks"
    moved = changed["features"][0]["stories"].pop(1)                      # FR-002 moves to F2
    changed["features"][1]["stories"].append(moved)
    backlog2, _ = ado_boards.normalize_backlog(changed, PRD)
    third = ado_boards.create_work_items("p", backlog2, again["items"])
    assert sorted(third["updated"]) == ["FR-001", "FR-002"] and third["created"] == []
    fr2 = boards.items[third["items"]["FR-002"]["id"]]
    assert len(fr2["relations"]) == 1 and fr2["relations"][0]["url"].endswith(f"/{third['items']['F2']['id']}")


def test_items_deleted_in_azure_boards_are_created_again_and_removed_requirements_are_kept(boards):
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    first = ado_boards.create_work_items("p", backlog)
    del boards.items[first["items"]["FR-003"]["id"]]
    items = dict(first["items"], **{"FR-003": dict(first["items"]["FR-003"], hash="stale")})
    items["FR-009"] = {"id": 999, "url": "u", "hash": "h"}
    result = ado_boards.create_work_items("p", backlog, items)
    assert result["created"] == ["FR-003"]
    assert result["not_in_prd"] == ["FR-009"]


def test_basic_process_has_no_feature_level_and_puts_criteria_in_the_description(boards):
    boards.process = "Basic"
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    result = ado_boards.create_work_items("p", backlog)
    assert result["created"] == ["EPIC", "FR-001", "FR-002", "FR-003"]
    story = boards.items[result["items"]["FR-001"]["id"]]
    assert story["type"] == "Issue"
    assert "Acceptance criteria" in _field(story["patch"], "System.Description")
    assert story["relations"][0]["url"].endswith(f"/{result['items']['EPIC']['id']}")


def test_errors_are_reported_and_partial_progress_is_kept(boards):
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    boards.fail_on = "Schedules"                                          # the F2 feature fails
    result = ado_boards.create_work_items("p", backlog)
    assert result["status"] == "partial"
    assert result["created"] == ["EPIC", "F1", "FR-001", "FR-002"]
    assert set(result["items"]) == {"EPIC", "F1", "FR-001", "FR-002"}

    boards.status = 403
    denied = ado_boards.create_work_items("p", backlog)
    assert denied["status"] == "error" and denied["error_type"] == "PERMISSION_DENIED"
    assert "Work Items (Read & write)" in denied["message"]


# ---------------------------------------------------------------------------- agent flow (fake LLM) + persistence

CONV = "conv-work-items"


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    project = store.ensure_project(CONV)
    store.update_project(project["project_id"], prd=PRD, prd_version=1, project_name="Clinic-Booking",
                         ado_project_id="proj-id", workflow_stage=store.STAGE_COMPLETE)
    return fake


class FakeAgent:
    """Answers by a function of the request payload (planner / writer), records every call."""
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def run(self, messages):
        payload = json.loads(messages[0].text)
        self.calls.append((payload, messages))
        text = self.answer(payload, len(messages))
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(messages=[SimpleNamespace(text=text)])


PLAN = {"epic": {"title": "Online clinic booking", "goal": "Patients book online.", "scope": "Booking.",
                 "success_criteria": []},
        "features": [{"key": "F1", "title": "Booking", "description": "Book appointments.", "out_of_scope": [],
                      "stories": [{"requirements": ["FR-001"], "domain": "Booking", "title": "Book a free slot"},
                                  {"requirements": ["FR-002"], "domain": "Notifications", "title": "Send reminders"}]},
                     {"key": "F2", "title": "Schedules", "description": "Manage schedules.", "out_of_scope": [],
                      "stories": [{"requirements": ["FR-003"], "domain": "Schedules", "title": "Manage schedules"}]}],
        "enablers": [], "tasks": "none", "rewrite": ["all"], "instruction": "", "change_summary": "Drafted."}


def _writer(payload, with_tasks=False):
    return json.dumps({"stories": [
        {"key": s["key"], "user_story": f"As a user, I want {s['title']} so that it works.", "context": "From the PRD.",
         "acceptance_criteria": [f"Given {s['key']}, when used, then it works"], "notes": "",
         "tasks": [{"layer": "Backend", "title": f"Implement {s['title']}", "description": "API.",
                    "done_when": "Tests pass."}] if with_tasks or payload.get("tasks") == "add" else []}
        for s in payload["stories"]]})


def _agents(monkeypatch, plan, writer=_writer):
    planner, writer_agent = FakeAgent(plan), FakeAgent(writer)
    monkeypatch.setattr(backlog_service, "get_backlog_planner", lambda client: planner)
    monkeypatch.setattr(backlog_service, "get_backlog_writer", lambda client: writer_agent)
    return planner, writer_agent


def _state():
    return store.get_workflow_state(CONV)


def test_draft_is_planned_then_written_per_feature_validated_stored_and_rendered(db, monkeypatch):
    planner, writer = _agents(monkeypatch, lambda payload, n: "not json" if n == 1 else json.dumps(PLAN))
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan the work items",
                                                              ["a long conversation"], _state()))
    assert len(planner.calls) == 2                                       # one repair attempt
    plan_request = planner.calls[0][0]
    assert plan_request["requirements"][0] == {"id": "FR-001", "text": PRD["Functional Requirements"][0]["requirement"]}
    assert "a long conversation" not in json.dumps(plan_request)          # no chat history (AGT-48)
    assert len(writer.calls) == 2                                         # one writer call per feature
    assert sorted(len(call[0]["stories"]) for call in writer.calls) == [1, 2]
    project = store.get_project(CONV)
    stories = [s for f in project["work_items_proposal"]["features"] for s in f["stories"]]
    assert [s["title"] for s in stories] == ["[FR-001] [Booking]: Book a free slot",
                                             "[FR-002] [Notifications]: Send reminders",
                                             "[FR-003] [Schedules]: Manage schedules"]
    assert stories[0]["parts"]["user_story"].startswith("As a user") and stories[0]["acceptance_criteria"]
    assert project["work_items_proposal_prd_version"] == 1
    assert reply.startswith("Here is the suggested **Azure Boards backlog** for **Clinic-Booking**")
    assert "1 epic, 2 features and 3 stories" in reply
    assert reply.rstrip().endswith("approve the backlog to create the work items in Azure Boards.")
    assert [s["label"] for s in wr.next_steps(_state())] == ["Approve and create Azure Boards items", "Request modifications to work items draft"]


def _draft_first(db, monkeypatch):
    _agents(monkeypatch, lambda payload, n: json.dumps(PLAN))
    asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan", [], _state()))


def test_update_rewrites_only_the_touched_stories(db, monkeypatch):
    _draft_first(db, monkeypatch)
    renamed = json.loads(json.dumps(PLAN))
    renamed["features"][1]["title"] = "Doctor Schedules"
    renamed["features"][1]["stories"][0]["title"] = "Manage doctors' weekly schedules"
    renamed.update(rewrite=["FR-003"], instruction="Add a criterion for overlapping slots", change_summary="Renamed F2.")
    planner, writer = _agents(monkeypatch, lambda payload, n: json.dumps(renamed))
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "UPDATE_WORK_ITEMS_PROPOSAL", "Rename F2", [],
                                                              _state()))
    outline = planner.calls[0][0]["current_outline"]
    assert [s["key"] for f in outline["features"] for s in f["stories"]] == ["FR-001", "FR-002", "FR-003"]
    assert "acceptance_criteria" not in json.dumps(outline)               # the outline has no long texts
    assert len(writer.calls) == 1 and [s["key"] for s in writer.calls[0][0]["stories"]] == ["FR-003"]
    assert writer.calls[0][0]["stories"][0]["current"]["acceptance_criteria"]   # the writer sees what to change
    assert writer.calls[0][0]["instruction"] == "Add a criterion for overlapping slots"
    assert reply.startswith("Here is the updated, suggested **Azure Boards backlog** for **Clinic-Booking**. Renamed F2.")
    stories = [s for f in store.get_project(CONV)["work_items_proposal"]["features"] for s in f["stories"]]
    assert stories[0]["parts"]["user_story"] == "As a user, I want Book a free slot so that it works."   # kept


def test_add_tasks_keeps_them_on_later_updates(db, monkeypatch):
    _draft_first(db, monkeypatch)
    with_tasks = dict(PLAN, tasks="add", rewrite=["all"], instruction="Add 2-5 tasks per story")
    _agents(monkeypatch, lambda payload, n: json.dumps(with_tasks))
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "UPDATE_WORK_ITEMS_PROPOSAL", "Add tasks", [],
                                                              _state()))
    assert "Task [Backend]: Implement Book a free slot" in reply
    # a later rename where the planner forgets to say "tasks": the existing tasks stay
    rename = dict(PLAN, tasks="none", rewrite=[], change_summary="Renamed.")
    _, writer = _agents(monkeypatch, lambda payload, n: json.dumps(rename))
    asyncio.run(backlog_service.run_backlog_operation(None, "UPDATE_WORK_ITEMS_PROPOSAL", "Rename", [], _state()))
    assert writer.calls == []                                             # nothing to rewrite
    stories = [s for f in store.get_project(CONV)["work_items_proposal"]["features"] for s in f["stories"]]
    assert all(s["tasks"] for s in stories)


def test_a_failed_step_saves_nothing_and_says_why_with_a_reference(db, monkeypatch):
    _agents(monkeypatch, lambda payload, n: json.dumps(PLAN), writer=lambda payload, n: RuntimeError("all busy"))
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan", [],
                                                              _state()))
    assert reply.startswith("I couldn't draft the work items this time because the AI service is busy")
    assert "(Reference: " in reply and store.get_project(CONV).get("work_items_proposal") in (None, {})


def test_create_stores_the_items_and_marks_the_draft_synced(db, monkeypatch):
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    store.update_project(store.get_project(CONV)["project_id"], work_items_proposal=backlog,
                         work_items_proposal_prd_version=1)
    items = {"EPIC": {"id": 1, "url": "https://dev.azure.com/o/p/_workitems/edit/1"}}
    monkeypatch.setattr(ado_boards, "create_work_items", lambda project, b, existing: {
        "status": "success", "items": items, "created": ["EPIC"], "updated": [], "unchanged": [], "not_in_prd": [],
        "types": {"epic": "Epic", "feature": "Feature", "story": "Product Backlog Item"},
        "backlog_url": "https://dev.azure.com/o/p/_backlogs/backlog"})
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "CREATE_WORK_ITEMS", "create them", [], _state()))
    project = store.get_project(CONV)
    assert project["work_items"] == items and project["work_items_prd_version"] == 1
    assert project["work_items_proposal"]["synced"] is True
    assert "[Open the backlog](https://dev.azure.com/o/p/_backlogs/backlog)" in reply
    assert "([#1](https://dev.azure.com/o/p/_workitems/edit/1))" in reply
    assert [s["label"] for s in wr.next_steps(_state())] == ["Summarize final project setup and status",
                                                         "Propose next development steps and milestones"]
    # a PRD change flags the work items as outdated and offers the update
    store.update_project(project["project_id"], prd_version=2)
    assert store.WORK_ITEMS_OUTDATED in store.outdated_items(_state())
    assert [s["label"] for s in wr.next_steps(_state())] == ["Update Azure Boards items from PRD"]
    # after redrafting from PRD v2 the draft is current: the next step is creating it (seen live: still "Update")
    store.update_project(project["project_id"], work_items_proposal=backlog, work_items_proposal_prd_version=2)
    assert store.WORK_ITEMS_OUTDATED not in store.outdated_items(_state())
    assert [s["label"] for s in wr.next_steps(_state())] == ["Approve and create Azure Boards items", "Request modifications to work items draft"]


def test_creating_again_says_nothing_was_created():
    # seen live: "Create the work items again" -> "(0 created, 19 unchanged)" read as if they had been created
    backlog, _ = ado_boards.normalize_backlog(DRAFT, PRD)
    result = {"status": "success", "items": {"EPIC": {"id": 1}}, "created": [], "updated": [],
              "unchanged": ["EPIC", "F1"], "not_in_prd": [], "types": {}, "backlog_url": "https://b"}
    reply = backlog_service.render_create_reply(backlog, result)
    assert reply.startswith("All 2 work items already exist in Azure Boards and match the current draft, so nothing "
                            "was created again (no duplicates).")
    assert "The work items are in Azure Boards" not in reply
    assert "[FR-001]" not in reply and "**Epic:**" not in reply         # AGT-30: no list repeated
    result.update(created=["F2"])                                        # something new -> the usual summary
    assert backlog_service.render_create_reply(backlog, result).startswith(
        "The work items are in Azure Boards (1 created, 2 unchanged)")


def test_partial_creation_is_kept_but_not_marked_synced(db, monkeypatch):
    store.update_project(store.get_project(CONV)["project_id"], work_items_proposal=DRAFT)
    monkeypatch.setattr(ado_boards, "create_work_items", lambda project, b, existing: {
        "status": "partial", "items": {"EPIC": {"id": 1}}, "created": ["EPIC"], "updated": [], "unchanged": [],
        "not_in_prd": [], "types": {}, "error": {"message": "Azure DevOps returned status 500."}})
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "CREATE_WORK_ITEMS", "create", [], _state()))
    project = store.get_project(CONV)
    assert project["work_items"] == {"EPIC": {"id": 1}} and not project["work_items_proposal"].get("synced")
    assert "will not be duplicated" in reply


def test_preconditions(db, monkeypatch):
    project = store.get_project(CONV)
    store.update_project(project["project_id"], ado_project_id=None)
    assert asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "x", [], _state())) \
        == backlog_service.NO_ADO_PROJECT_MESSAGE
    store.update_project(project["project_id"], ado_project_id="proj-id")
    assert asyncio.run(backlog_service.run_backlog_operation(None, "CREATE_WORK_ITEMS", "x", [], _state())) \
        == backlog_service.NO_DRAFT_MESSAGE
    legacy = {"project": {k: v for k, v in store.get_project(CONV).items() if not k.startswith("work_items")}}
    assert asyncio.run(backlog_service.run_backlog_operation(None, "CREATE_WORK_ITEMS", "x", [], legacy)) \
        == backlog_service.MIGRATION_006_MESSAGE


# ---------------------------------------------------------------------------- routing

def test_backlog_operations_need_the_ado_project_and_a_draft(db):
    ops = wr.allowed_operations(store.STAGE_COMPLETE, _state())
    assert "GENERATE_WORK_ITEMS_PROPOSAL" in ops and "CREATE_WORK_ITEMS" not in ops
    store.update_project(store.get_project(CONV)["project_id"], work_items_proposal=DRAFT)
    ops = wr.allowed_operations(store.STAGE_COMPLETE, _state())
    assert {"UPDATE_WORK_ITEMS_PROPOSAL", "CREATE_WORK_ITEMS"} <= set(ops)
    store.update_project(store.get_project(CONV)["project_id"], ado_project_id=None)
    assert not {"GENERATE_WORK_ITEMS_PROPOSAL", "CREATE_WORK_ITEMS"} & set(
        wr.allowed_operations(store.STAGE_COMPLETE, _state()))
    assert wr.OPERATION_AGENT["CREATE_WORK_ITEMS"] == "Backlog_Agent"


def test_approval_alone_does_not_create_work_items():
    decision = wr.RoutingDecision(reasoning="r", user_intent="approve_only", action="delegate", agent="Backlog_Agent",
                                  operation="CREATE_WORK_ITEMS")
    corrected = wr.apply_intent_rules(decision, store.STAGE_COMPLETE, {})
    assert corrected.action == "respond" and "create the work items in Azure Boards" in corrected.reply


# ---------------------------------------------------------------------------- ADO-3: board titles, grouping, tasks

PRD5 = {"Objective": "Let patients book clinic appointments online.",
        "Goals": [{"goal": "80% of bookings online", "metric": "share of bookings"}],
        "Functional Requirements": [
            {"id": "FR-001", "requirement": "The system shall let patients register and sign in."},
            {"id": "FR-002", "requirement": "The system shall let patients filter slots by doctor and date."},
            {"id": "FR-003", "requirement": "The system shall let patients book a free slot."},
            {"id": "FR-004", "requirement": "The system shall email a reminder 24 hours before."},
            {"id": "FR-005", "requirement": "The system shall let receptionists manage doctors' schedules."}]}
GROUPED = {"epic": {"title": "[EPIC] Patient Online Booking & Schedule Management", "goal": "Patients book online.",
                    "scope": "Booking, reminders and schedules.", "success_criteria": ["80% of bookings online"]},
           "features": [
               {"key": "F1", "title": "Patient Booking Wizard", "description": "Patients find and book a slot.",
                "out_of_scope": ["Payments"],
                "stories": [
                    {"requirements": ["FR-002", "FR-003", "FR-099"], "domain": "booking",
                     "title": "As a patient, I want to filter slots by doctor and date and book one so that I save time",
                     "user_story": "As a patient, I want to book online so that I save time.",
                     "context": "Slots are 15 minutes.", "acceptance_criteria": ["Given a free slot, when I book, then it is mine"],
                     "notes": "", "tasks": [
                         {"layer": "backend", "title": "Implement GET /api/v1/doctors/slots query",
                          "description": "Return free slots.", "done_when": "Slots are filtered by doctor and date."},
                         {"layer": "Frontend", "title": "Build calendar slot picker component", "description": "",
                          "done_when": ""}]},
                    {"requirements": ["FR-003", "FR-004"], "domain": "Notifications",
                     "title": "Dispatch automated 24h prior reminder email"}]},
               {"key": "F2", "title": "Doctor Schedules", "stories": [
                   {"requirements": ["FR-005"], "domain": "Schedules", "title": "[FR-005] [Schedules]: Edit weekly schedules"}]}]}


def test_stories_group_requirements_with_short_board_titles():
    backlog, notes = ado_boards.normalize_backlog(GROUPED, PRD5)
    stories = [s for f in backlog["features"] for s in f["stories"]]
    # every requirement exactly once (FR-003 kept in its first story, FR-099 dropped, FR-001 missing -> added)
    covered = [r for s in stories for r in s["requirements"]]
    assert sorted(covered) == ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"] and len(covered) == 5
    assert any("FR-099" in n for n in notes) and any("FR-001" in n for n in notes)
    titles = [s["title"] for s in stories]
    assert titles[0] == "[FR-002, FR-003] [Booking]: Filter slots by doctor and date and book one"
    assert titles[1] == "[FR-004] [Notifications]: Dispatch automated 24h prior reminder email"
    assert titles[2] == "[FR-005] [Schedules]: Edit weekly schedules"                   # model's prefix not doubled
    assert titles[3].startswith("[FR-001] [General]: ")
    for story in stories:
        action = story["title"].split("]: ", 1)[1]
        assert len(action.split()) <= ado_boards.MAX_ACTION_WORDS and not action.lower().startswith("as a")
    assert backlog["epic"]["title"] == "Patient Online Booking & Schedule Management"     # no brackets
    assert [f["title"] for f in backlog["features"]][:2] == ["Patient Booking Wizard", "Doctor Schedules"]


def test_descriptions_are_thorough_and_rendered_as_html():
    backlog, _ = ado_boards.normalize_backlog(GROUPED, PRD5)
    story = backlog["features"][0]["stories"][0]
    for part in ("**User story:** As a patient, I want to book online", "**Context:** Slots are 15 minutes.",
                 "**Requirements covered:**", "- FR-002: The system shall let patients filter slots"):
        assert part in story["description"]
    epic = backlog["epic"]["description"]
    assert "**Goal:** Patients book online." in epic and "**Success criteria:**" in epic and "- 80% of bookings" in epic
    assert "**Out of scope:**" in backlog["features"][0]["description"]
    html_text = ado_boards._html(story["description"])
    assert "<b>User story:</b>" in html_text and "<ul><li>FR-002: The system shall" in html_text
    assert "**" not in html_text and "<script" not in ado_boards._html("<script>alert(1)</script>")


def test_tasks_are_created_as_children_with_their_layer(boards):
    backlog, _ = ado_boards.normalize_backlog(GROUPED, PRD5)
    tasks = backlog["features"][0]["stories"][0]["tasks"]
    assert [t["title"] for t in tasks] == ["[Backend]: Implement GET /api/v1/doctors/slots query",
                                           "[Frontend]: Build calendar slot picker component"]
    assert tasks[0]["key"] == "FR-002-T1" and "**Done when:** Slots are filtered" in tasks[0]["description"]
    result = ado_boards.create_work_items("proj-id", backlog)
    assert result["status"] == "success" and "FR-002-T1" in result["created"]
    task = boards.items[result["items"]["FR-002-T1"]["id"]]
    assert task["type"] == "Task" and _field(task["patch"], "System.Tags") == "DevFoundry; Backend"
    assert task["relations"][0]["url"].endswith(f"/_apis/wit/workItems/{result['items']['FR-002']['id']}")
    story = boards.items[result["items"]["FR-002"]["id"]]
    assert _field(story["patch"], "System.Tags") == "DevFoundry; FR-002; FR-003; Booking"


def test_backlog_prompts_ask_for_parts_not_full_titles():
    from app.services.agents.backlog_agent import PLANNER_PROMPT, WRITER_PROMPT
    for rule in ("2-3 features", "groups 1-4 closely related requirements", "\"tasks\": \"add\" when the user asks",
                 "the system adds \"[FR-ids] [Domain]: \"", "\"rewrite\""):
        assert rule in PLANNER_PROMPT
    for rule in ("\"user_story\"", "\"done_when\"", "Never vague", "never change it"):
        assert rule in WRITER_PROMPT


def test_technical_enablers_and_uncovered_goals():
    prd = dict(PRD5, Goals=["80% of bookings made online within 3 months",
                            "p95 page load under 2 s", "99.5% availability during clinic hours"])
    draft = dict(GROUPED, enablers=[
        {"title": "Add caching and a performance budget for slot search", "goal_numbers": [2, 9],
         "description": "Keep page loads fast.", "acceptance_criteria": ["p95 under 2 s in a load test"]},
        {"title": "Set up the CI pipeline with tests per pull request", "goal_numbers": []}])
    backlog, _ = ado_boards.normalize_backlog(draft, prd)
    enablers = backlog["features"][-1]
    assert enablers["title"] == "Technical enablers"
    assert [s["title"] for s in enablers["stories"]] == [
        "[TE-1] [Enabler]: Add caching and a performance budget for slot search",
        "[TE-2] [Enabler]: Set up the CI pipeline with tests per pull request"]
    assert "**Supports:**\n- Goal 2: p95 page load under 2 s" in enablers["stories"][0]["description"]
    # goal 3 (availability) is non-functional and no enabler supports it -> named; goal 1 is a product outcome
    assert backlog["uncovered_goals"] == ["Goal 3: 99.5% availability during clinic hours"]
    reply = backlog_service.render_draft_reply({"project_name": "Clinic", "prd": prd, "prd_version": 1}, backlog,
                                               "GENERATE_WORK_ITEMS_PROPOSAL")
    assert "Not covered by any work item yet: Goal 3: 99.5% availability" in reply
    # every FR is still in exactly one story; enablers have no requirement ids
    covered = [r for f in backlog["features"] for s in f["stories"] for r in s["requirements"]]
    assert sorted(covered) == ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"]


def test_a_plan_that_misses_requirements_is_asked_again_then_completed(db, monkeypatch):
    # seen live (GymPass): the planner left out 5 of 16 requirements; they ended up without acceptance criteria
    partial = json.loads(json.dumps(PLAN))
    partial["features"] = partial["features"][:1]                         # FR-003 missing
    planner, writer = _agents(monkeypatch, lambda payload, n: json.dumps(PLAN if "correction" in payload else partial))
    asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan", [], _state()))
    assert len(planner.calls) == 2 and "FR-003" in planner.calls[1][0]["correction"]
    stories = [s for f in store.get_project(CONV)["work_items_proposal"]["features"] for s in f["stories"]]
    assert all(s["acceptance_criteria"] for s in stories)


def test_requirements_still_missing_get_their_own_written_stories(db, monkeypatch):
    partial = json.loads(json.dumps(PLAN))
    partial["features"] = partial["features"][:1]
    planner, writer = _agents(monkeypatch, lambda payload, n: json.dumps(partial))
    asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan", [], _state()))
    features = store.get_project(CONV)["work_items_proposal"]["features"]
    other = next(f for f in features if f["title"] == "Other requirements")
    assert [s["key"] for s in other["stories"]] == ["FR-003"] and other["stories"][0]["acceptance_criteria"]
    assert any(call[0]["feature"]["title"] == "Other requirements" for call in writer.calls)
