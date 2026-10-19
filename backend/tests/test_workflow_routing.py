"""Routing decision guard, document versions / outdated detection and next-step suggestions (no network)."""
import json
from types import SimpleNamespace

import pytest

from app.services import conversation_service as cs
from app.services import project_store as store
from app.services import workflow_routing as wr
from tests.fake_supabase import FakeSupabase

pytestmark = pytest.mark.usefixtures("no_real_network")

CONV = "conv-routing"
PRD = {"Objective": "Clinic booking"}
TRD = {"Database Technology Stack": {"database": "PostgreSQL"}}


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    return fake


def save_prd(prd=PRD):
    cs.persist_requirements_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PRD",
                                                     "structured_project_response": prd}))


def save_trd(trd=TRD):
    cs.persist_requirements_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "TRD",
                                                     "technical_requirements_document": trd}))


def state():
    return store.get_workflow_state(CONV)


# ---------------------------------------------------------------------------
# Stages and allowed operations
# ---------------------------------------------------------------------------

def test_effective_stage_handles_missing_and_legacy_rows():
    assert wr.effective_stage(None) is None
    assert wr.effective_stage({"project": None, "repository": None}) == store.STAGE_REQUIREMENTS
    legacy = {"project": {"workflow_stage": "REQUIREMENTS", "prd": PRD, "trd": TRD}}
    assert wr.effective_stage(legacy) == store.STAGE_TRD_REVIEW
    assert wr.effective_stage({"project": {"workflow_stage": "REQUIREMENTS", "prd": PRD}}) == store.STAGE_PRD_REVIEW
    assert wr.effective_stage({"error": "tables missing"}) is None


def test_allowed_operations_per_stage():
    docs = {"project": {"prd": PRD, "trd": TRD}}
    assert wr.allowed_operations(store.STAGE_REQUIREMENTS, {"project": None}) == ["GENERATE_PRD", "UPDATE_PRD"]
    prd_review = wr.allowed_operations(store.STAGE_PRD_REVIEW, {"project": {"prd": PRD}})
    assert "GENERATE_TRD" in prd_review and "UPDATE_PRD" in prd_review
    assert "GENERATE_PROJECT_PROPOSAL" not in prd_review                  # no skipping ahead
    trd_review = wr.allowed_operations(store.STAGE_TRD_REVIEW, docs)
    assert {"UPDATE_TRD", "UPDATE_PRD", "GENERATE_PROJECT_PROPOSAL"} <= set(trd_review)
    assert "CREATE_PROJECT" not in trd_review
    # Documents can be updated in every later stage
    for stage in (store.STAGE_PROJECT_REVIEW, store.STAGE_REPOSITORY_REVIEW, store.STAGE_STRUCTURE_REVIEW, store.STAGE_COMPLETE):
        assert {"UPDATE_PRD", "UPDATE_TRD"} <= set(wr.allowed_operations(stage, docs)), stage
    complete = wr.allowed_operations(store.STAGE_COMPLETE, docs)
    assert {"UPDATE_ADO_PROJECT", "UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"} <= set(complete)
    assert "CREATE_PROJECT" not in complete and "CREATE_REPOSITORY" not in complete
    assert set(wr.allowed_operations(None)) == set(wr.OPERATION_AGENT)    # no stored state -> not gated


def test_validate_decision_blocks_skipped_steps_and_fixes_agent():
    skip = wr.RoutingDecision(reasoning="r", action="delegate", agent="Repo_Setup_Agent", operation="GENERATE_PROJECT_PROPOSAL")
    reason = wr.validate_decision(skip, store.STAGE_PRD_REVIEW, {"project": {"prd": PRD}})
    assert reason and "isn't available" in wr.blocked_reply(store.STAGE_PRD_REVIEW, reason)
    assert "create the Technical Requirements Document" in wr.blocked_reply(store.STAGE_PRD_REVIEW, reason)

    wrong_agent = wr.RoutingDecision(reasoning="r", action="delegate", agent="Repo_Setup_Agent", operation="UPDATE_TRD")
    assert wr.validate_decision(wrong_agent, store.STAGE_COMPLETE, {"project": {"prd": PRD, "trd": TRD}}) is None
    assert wrong_agent.agent == "Requirements_Agent"
    assert wr.validate_decision(wr.RoutingDecision(reasoning="r", action="respond", reply="hi"), store.STAGE_COMPLETE) is None


def decision(**fields):
    return wr.RoutingDecision(reasoning="r", **fields)


def test_approval_alone_does_not_move_to_the_next_document():
    docs = {"project": {"prd": PRD, "trd": TRD}}
    for op, stage, state_ in [("GENERATE_TRD", store.STAGE_PRD_REVIEW, {"project": {"prd": PRD}}),
                              ("GENERATE_PROJECT_PROPOSAL", store.STAGE_TRD_REVIEW, docs)]:
        approve = decision(action="delegate", agent=wr.OPERATION_AGENT[op], operation=op, user_intent="approve_only")
        corrected = wr.apply_intent_rules(approve, stage, state_)
        assert corrected.action == "respond" and corrected.reply.endswith("?")
        explicit = decision(action="delegate", agent=wr.OPERATION_AGENT[op], operation=op, user_intent="explicit_next_step")
        assert wr.apply_intent_rules(explicit, stage, state_) is explicit
    # Approving a proposal does create it (approval is the trigger for create/push steps)
    create = decision(action="delegate", agent="Repo_Setup_Agent", operation="CREATE_PROJECT", user_intent="approve_only")
    assert wr.apply_intent_rules(create, store.STAGE_PROJECT_REVIEW, docs) is create


def test_technology_change_request_becomes_trd_update():
    docs = {"project": {"prd": PRD, "trd": TRD}}
    answered = decision(action="respond", reply="Already PostgreSQL.", user_intent="change_request", mentions_technology=True)
    corrected = wr.apply_intent_rules(answered, store.STAGE_TRD_REVIEW, docs)
    assert (corrected.action, corrected.agent, corrected.operation) == ("delegate", "Requirements_Agent", "UPDATE_TRD")
    # Not without a TRD, and not for questions
    assert wr.apply_intent_rules(answered, store.STAGE_PRD_REVIEW, {"project": {"prd": PRD}}) is answered
    question = decision(action="respond", reply="x", user_intent="question", mentions_technology=True)
    assert wr.apply_intent_rules(question, store.STAGE_TRD_REVIEW, docs) is question
    # A delegated structure change mentioning a technology is left alone
    structure = decision(action="delegate", agent="Repo_Setup_Agent", operation="UPDATE_REPOSITORY_STRUCTURE",
                         user_intent="change_request", mentions_technology=True)
    assert wr.apply_intent_rules(structure, store.STAGE_STRUCTURE_REVIEW, docs) is structure


def test_parse_routing_decision_value_text_and_fallback():
    decision = wr.RoutingDecision(reasoning="r", action="respond", reply="hello")
    assert wr.parse_routing_decision(SimpleNamespace(value=decision, messages=[])) is decision
    text = SimpleNamespace(value=None, messages=[SimpleNamespace(
        text='{"reasoning": "r", "action": "delegate", "agent": "Requirements_Agent", "operation": "UPDATE_PRD"}')])
    assert wr.parse_routing_decision(text).operation == "UPDATE_PRD"
    plain = SimpleNamespace(value=None, messages=[SimpleNamespace(text="Just a plain answer")])
    parsed = wr.parse_routing_decision(plain)
    assert parsed.action == "respond" and parsed.reply == "Just a plain answer"


# ---------------------------------------------------------------------------
# Document versions and outdated artifacts
# ---------------------------------------------------------------------------

def test_requirements_stages_and_versions(db):
    save_prd()
    project = store.get_project(CONV)
    assert (project["workflow_stage"], project["prd_version"]) == (store.STAGE_PRD_REVIEW, 1)
    save_trd()
    project = store.get_project(CONV)
    assert (project["workflow_stage"], project["trd_version"], project["trd_prd_version"]) == (store.STAGE_TRD_REVIEW, 1, 1)
    assert store.outdated_items(state()) == []

    save_prd({"Objective": "Clinic booking + SMS"})                       # PRD changed during TRD review
    project = store.get_project(CONV)
    assert project["workflow_stage"] == store.STAGE_TRD_REVIEW               # stage does not move backwards
    assert store.outdated_items(state()) == [store.TRD_OUTDATED]
    save_trd({"Database Technology Stack": {"database": "PostgreSQL"}, "Integrations": ["Twilio"]})
    assert store.outdated_items(state()) == []


def test_document_updates_after_repo_setup_flag_downstream_artifacts(db):
    save_prd()
    save_trd()
    project = store.get_project(CONV)
    store.update_project(project["project_id"], project_name="Clinic", ado_project_id="proj-guid",
                         proposal_trd_version=1, workflow_stage=store.STAGE_STRUCTURE_REVIEW)
    repo = store.propose_repository_name(project["project_id"], "clinic")
    store.save_structure(repo["repo_id"], [{"path": "/README.md", "content": "# c\n"}], 0, trd_version=1)

    save_prd({"Objective": "Clinic booking with payments"})
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_STRUCTURE_REVIEW   # repo setup is not undone
    assert store.outdated_items(state()) == [store.TRD_OUTDATED]

    save_trd({"Database Technology Stack": {"database": "PostgreSQL"}, "Payments": "Stripe"})
    assert store.outdated_items(state()) == [store.ADO_PROJECT_OUTDATED, store.STRUCTURE_OUTDATED]
    assert "outdated: ADO_PROJECT_OUTDATED" in store.describe_workflow_state(state())


def test_project_proposal_outdated_before_creation(db):
    save_prd()
    save_trd()
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PROJECT_REVIEW",
                                                   "project_proposal": {"project_name": "Clinic", "project_description": "d"}}))
    assert store.get_project(CONV)["proposal_trd_version"] == 1
    save_trd({"Database Technology Stack": {"database": "MySQL"}})
    assert store.outdated_items(state()) == [store.PROJECT_PROPOSAL_OUTDATED]
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_PROJECT_REVIEW


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def labels(suggestions):
    return [s["label"] for s in suggestions]


def test_next_steps_follow_the_workflow(db):
    assert wr.next_steps(None) == []
    save_prd()
    assert labels(wr.next_steps(state())) == ["Approve the PRD and create TRD", "Request custom adjustments to the PRD"]
    save_trd()
    assert labels(wr.next_steps(state())) == ["Approve the TRD and start project setup", "Adjust architecture and technology stack choices"]
    project = store.get_project(CONV)
    store.update_project(project["project_id"], project_name="Clinic", proposal_trd_version=1,
                         workflow_stage=store.STAGE_PROJECT_REVIEW)
    assert labels(wr.next_steps(state())) == ["Approve proposal and create ADO project", "Modify proposed project name and description"]
    store.update_project(project["project_id"], ado_project_id="p", workflow_stage=store.STAGE_REPOSITORY_REVIEW)
    assert labels(wr.next_steps(state())) == ["Suggest suitable Git repository name options", "Propose a custom Git repository name"]
    repo = store.propose_repository_name(project["project_id"], "clinic")
    assert labels(wr.next_steps(state())) == ["Create repository with the proposed name", "Propose a custom Git repository name"]
    store.save_structure(repo["repo_id"], [{"path": "/README.md", "content": "# c\n"}], 0, trd_version=1)
    store.update_project(project["project_id"], workflow_stage=store.STAGE_STRUCTURE_REVIEW)
    assert labels(wr.next_steps(state())) == ["Approve proposed structure and push to main", "Request folder structure or file adjustments",
                                             "Verify the scaffold installs and builds"]
    pushed = [{"path": "/README.md", "content": "# c\n"}]
    store.update_repository(repo["repo_id"], status=store.REPO_PUSHED, pushed_structure=pushed)
    store.update_project(project["project_id"], workflow_stage=store.STAGE_COMPLETE)
    assert labels(wr.next_steps(state())) == ["Draft project work items in Boards", "Summarize final project setup and status"]          # Azure Boards (migration 006)
    store.save_structure(repo["repo_id"], [{"path": "/README.md", "content": "# c v2\n"}], 1, trd_version=1)
    assert labels(wr.next_steps(state())) == ["Push folder structure changes to main", "Draft project work items in Boards"]


def test_next_steps_put_outdated_items_first_and_hold_the_workflow(db):
    save_prd()
    save_trd()
    project = store.get_project(CONV)
    store.update_project(project["project_id"], project_name="Clinic", ado_project_id="p", proposal_trd_version=1,
                         workflow_stage=store.STAGE_STRUCTURE_REVIEW)
    repo = store.propose_repository_name(project["project_id"], "clinic")
    store.save_structure(repo["repo_id"], [{"path": "/README.md", "content": "# c\n"}], 0, trd_version=1)

    save_prd({"Objective": "changed"})
    assert labels(wr.next_steps(state())) == ["Update TRD to match latest PRD"]           # no "Push to main" while upstream is outdated
    save_trd({"changed": True})
    steps = wr.next_steps(state())
    assert labels(steps) == ["Update Azure DevOps project with requirements", "Update repository structure to match TRD"]
    assert all(s["prompt"] for s in steps)
    assert "outdated" in cs.follow_up_note(CONV) and "repository structure" in cs.follow_up_note(CONV)


def test_get_suggestions_is_safe_without_tables(monkeypatch):
    monkeypatch.setattr(store, "client_factory", lambda: FakeSupabase(missing_tables={"Project", "Repository"}))
    assert cs.get_suggestions(CONV) == []
    assert cs.get_suggestions(None) == []


# ---------------------------------------------------------------------------
# PRD readiness guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("output, expected", [
    ({"status": "COMPLETE", "readiness": {"R1": "provided", "R2": "missing", "R3": "provided", "R4": "missing", "R5": "provided"}},
     ["R2", "R4"]),
    ({"status": "COMPLETE", "readiness": {"R1": "provided", "R2": "provided", "R3": "provided", "R4": "assumed", "R5": "declined"}}, []),
    ({"status": "CLARIFICATION_REQUIRED", "readiness": {"R1": "missing"}}, []),   # asking is the right reaction
    ({"status": "COMPLETE", "readiness": None}, []),                              # UPDATE_PRD / older outputs
    ({"status": "COMPLETE"}, []),
])
def test_missing_readiness_items(output, expected):
    assert cs.missing_readiness_items(json.dumps(output)) == expected
