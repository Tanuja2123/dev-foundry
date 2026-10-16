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
    assert "**Feature F1: Booking**" in text
    assert "- **FR-001** As a patient, I want to book so that I save time ([#7](https://dev.azure.com/o/p/_workitems/edit/7))" in text
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
    assert _field(story["patch"], "System.Tags") == "DevFoundry; FR-001"
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
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    async def run(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(messages=[SimpleNamespace(text=self.outputs.pop(0))])


def _state():
    return store.get_workflow_state(CONV)


def test_draft_is_validated_stored_and_rendered(db, monkeypatch):
    agent = FakeAgent(["not json", json.dumps({"status": "COMPLETE", "backlog": DRAFT, "change_summary": "Drafted."})])
    monkeypatch.setattr(backlog_service, "get_backlog_agent", lambda client: agent)
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "GENERATE_WORK_ITEMS_PROPOSAL", "Plan the work items",
                                                              [], _state()))
    assert len(agent.calls) == 2                                          # one repair attempt
    assert "approved_prd_json" in agent.calls[0][-1].text
    project = store.get_project(CONV)
    assert project["work_items_proposal"]["features"][0]["key"] == "F1"
    assert project["work_items_proposal_prd_version"] == 1
    assert reply.startswith("Here is the suggested **Azure Boards backlog** for **Clinic-Booking**")
    assert "1 epic, 2 features and 3 stories" in reply
    assert reply.rstrip().endswith("approve the backlog to create the work items in Azure Boards.")
    assert [s["label"] for s in wr.next_steps(_state())] == ["Approve and create Azure Boards items", "Request modifications to work items draft"]


def test_update_sends_the_current_draft(db, monkeypatch):
    store.update_project(store.get_project(CONV)["project_id"], work_items_proposal=DRAFT)
    agent = FakeAgent([json.dumps({"status": "COMPLETE", "backlog": DRAFT, "change_summary": "Renamed F2."})])
    monkeypatch.setattr(backlog_service, "get_backlog_agent", lambda client: agent)
    reply = asyncio.run(backlog_service.run_backlog_operation(None, "UPDATE_WORK_ITEMS_PROPOSAL", "Rename F2", [],
                                                              _state()))
    assert "current_work_items_draft_json" in agent.calls[0][-1].text
    assert reply.startswith("Here is the updated, suggested **Azure Boards backlog** for **Clinic-Booking**. Renamed F2.")


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
    assert reply.startswith("These work items already exist in Azure Boards and match the current draft, so nothing "
                            "was created again (no duplicates).")
    assert "The work items are in Azure Boards" not in reply
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
