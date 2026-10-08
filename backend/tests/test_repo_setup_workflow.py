"""Project/Repository persistence, conversation-bound Repo Setup tools and orchestrator helpers (no network)."""
import asyncio
import json

import pytest

from app.services import conversation_service as cs
from app.services import project_store as store
from app.services.agents import repo_setup_agent as rsa
from tests.fake_supabase import FakeSupabase

CONV = "conv-123"
TRD = {"Backend Technology Stack": {"language": "Go", "framework": "Gin"}}
STRUCTURE = [
    {"path": "/README.md", "content": "# svc\n"},
    {"path": "/cmd/server/main.go", "content": "package main\n"},
    {"path": "/internal/handlers/.gitkeep", "content": "keep\n"},
]


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    return fake


@pytest.fixture
def ado(monkeypatch):
    """Fakes the ADO MCP functions used by the agent tools and records their calls."""
    calls = []

    def create_project(project_name, project_description):
        calls.append(("create_ado_project", project_name))
        return {"status": "success", "name": project_name, "description": project_description,
                "id": "proj-guid", "url": f"https://dev.azure.com/org/{project_name}"}

    def create_repo(project_id_or_name, repository_name):
        calls.append(("create_ado_repository", project_id_or_name, repository_name))
        return {"status": "success", "name": repository_name, "id": "repo-guid",
                "url": f"https://dev.azure.com/org/{project_id_or_name}/_git/{repository_name}"}

    def push(project_id_or_name, repository_name, files, commit_message=None, branch=None):
        calls.append(("push_repository_files", project_id_or_name, repository_name, json.loads(json.dumps(files)),
                      commit_message))
        return {"status": "success", "commit_id": "c0ffee", "commit_message": commit_message or "chore: initial project scaffold",
                "branch": "main", "files_count": len(files), "files": sorted(f["path"] for f in files),
                "commit_url": "https://dev.azure.com/org/p/_git/r/commit/c0ffee"}

    monkeypatch.setattr(rsa, "create_ado_project", create_project)
    monkeypatch.setattr(rsa, "create_ado_repository", create_repo)
    monkeypatch.setattr(rsa, "push_repository_files", push)
    return calls


def tools(conversation_id=CONV):
    return {t.name: t for t in rsa.build_repo_setup_tools(conversation_id)}


def call(tool_map, name, **arguments):
    result = asyncio.run(tool_map[name].invoke(arguments=arguments))
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# project_store
# ---------------------------------------------------------------------------

def test_ensure_project_is_idempotent(db):
    first = store.ensure_project(CONV)
    second = store.ensure_project(CONV)
    assert first["project_id"] == second["project_id"]
    assert first["workflow_stage"] == store.STAGE_REQUIREMENTS
    assert len(db.tables["Project"]) == 1


def test_propose_repository_name_renames_pending_proposal_only(db):
    project = store.ensure_project(CONV)
    a = store.propose_repository_name(project["project_id"], "svc")
    b = store.propose_repository_name(project["project_id"], "svc-api")
    assert a["repo_id"] == b["repo_id"] and b["repo_name"] == "svc-api"
    store.update_repository(b["repo_id"], status=store.REPO_CREATED)
    c = store.propose_repository_name(project["project_id"], "another-repo")
    assert c["repo_id"] != b["repo_id"]                    # created repos are never renamed
    assert store.get_active_repository(project["project_id"])["repo_id"] == c["repo_id"]


def test_missing_tables_give_migration_hint(monkeypatch):
    fake = FakeSupabase(missing_tables={"Project", "Repository"})
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    with pytest.raises(store.StoreError, match="001_create_project_and_repository.sql"):
        store.get_project(CONV)
    state = store.get_workflow_state(CONV)
    assert state["project"] is None and "001_create_project_and_repository.sql" in state["error"]
    assert "unavailable" in store.describe_workflow_state(state)


def test_describe_workflow_state_includes_requested_context(db):
    project = store.ensure_project(CONV)
    store.update_project(project["project_id"], trd=TRD, prd={"Objective": "x"}, project_name="Svc")
    repo = store.propose_repository_name(project["project_id"], "svc")
    store.save_structure(repo["repo_id"], STRUCTURE, 0)
    state = store.get_workflow_state(CONV)
    short = store.describe_workflow_state(state)
    full = store.describe_workflow_state(state, include_requirements=True, include_structure=True)
    assert "repository_name: svc" in short and "structure_version: 1 (3 files)" in short
    assert "approved_trd_json" not in short and "Gin" in full and "/cmd/server/main.go" in full


# ---------------------------------------------------------------------------
# Conversation-bound tools: full lifecycle
# ---------------------------------------------------------------------------

def test_full_lifecycle_pushes_exactly_the_reviewed_structure(db, ado):
    t = tools()
    store.ensure_project(CONV)

    created = call(t, "create_ado_project", project_name="Svc", project_description="Go service")
    assert created["status"] == "success"
    project = store.get_project(CONV)
    assert project["ado_project_id"] == "proj-guid" and project["workflow_stage"] == store.STAGE_REPOSITORY_REVIEW

    repo = call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")
    assert repo["status"] == "success"
    stored_repo = store.get_active_repository(project["project_id"])
    assert stored_repo["status"] == store.REPO_CREATED and stored_repo["ado_repo_id"] == "repo-guid"

    saved = call(t, "save_repository_structure", files=STRUCTURE)
    assert saved["structure_version"] == 1 and saved["files_count"] == 3

    updated = call(t, "update_repository_structure",
                   upsert_files=[{"path": "/Makefile", "content": "build:\n\tgo build ./...\n"}],
                   remove_paths=["/internal/handlers"])
    assert updated["structure_version"] == 2
    assert updated["paths"] == ["/Makefile", "/README.md", "/cmd/server/main.go"]

    # What the user sees is rendered from the stored row ...
    shown = cs.render_structure_for_display(store.get_workflow_state(CONV))
    assert "version 2, 3 files" in shown and "Makefile" in shown and "handlers" not in shown

    pushed = call(t, "push_repository_structure", commit_message="Initial scaffold")
    assert pushed["status"] == "success"
    # ... and the push sends exactly that stored row
    _, project_name, repo_name, files, message = ado[-1]
    stored = store.get_active_repository(project["project_id"])
    assert (project_name, repo_name, message) == ("Svc", "svc", "Initial scaffold")
    assert files == stored["structure"]
    assert stored["status"] == store.REPO_PUSHED and stored["commit_id"] == "c0ffee"
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_COMPLETE
    assert [c[0] for c in ado] == ["create_ado_project", "create_ado_repository", "push_repository_files"]


def test_guards_prevent_out_of_order_and_duplicate_actions(db, ado):
    t = tools()
    store.ensure_project(CONV)
    assert call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")["status"] == "error"
    assert call(t, "save_repository_structure", files=STRUCTURE)["status"] == "error"
    assert call(t, "push_repository_structure")["status"] == "error"

    call(t, "create_ado_project", project_name="Svc", project_description="d")
    again = call(t, "create_ado_project", project_name="Svc", project_description="d")
    assert again["error_type"] == "ALREADY_CREATED"

    call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")
    assert call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")["error_type"] == "ALREADY_CREATED"
    assert call(t, "push_repository_structure")["message"] == "There is no saved structure to push."
    assert call(t, "update_repository_structure", remove_paths=["/x"])["status"] == "error"   # nothing saved yet

    call(t, "save_repository_structure", files=STRUCTURE)
    call(t, "push_repository_structure")
    for name, args in [("save_repository_structure", {"files": STRUCTURE}),
                       ("update_repository_structure", {"remove_paths": ["/README.md"]}),
                       ("push_repository_structure", {})]:
        result = call(t, name, **args)
        assert result["status"] == "error" and "already pushed" in result["message"]
    assert [c[0] for c in ado].count("push_repository_files") == 1
    assert [c[0] for c in ado].count("create_ado_project") == 1


def test_invalid_structure_is_rejected_and_not_saved(db, ado):
    t = tools()
    store.ensure_project(CONV)
    call(t, "create_ado_project", project_name="Svc", project_description="d")
    call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")
    bad = call(t, "save_repository_structure", files=[{"path": "/ok.txt", "content": "ok"},
                                                      {"path": "/empty/.gitkeep", "content": " "}])
    assert bad["error_type"] == "VALIDATION_ERROR"
    assert store.get_active_repository(store.get_project(CONV)["project_id"]).get("structure") is None


def test_failed_push_keeps_structure_for_review(db, ado, monkeypatch):
    t = tools()
    store.ensure_project(CONV)
    call(t, "create_ado_project", project_name="Svc", project_description="d")
    call(t, "create_ado_repository", project_id_or_name="Svc", repository_name="svc")
    call(t, "save_repository_structure", files=STRUCTURE)
    monkeypatch.setattr(rsa, "push_repository_files",
                        lambda **kwargs: {"status": "error", "error_type": "PERMISSION_DENIED", "message": "denied"})
    assert call(t, "push_repository_structure")["status"] == "error"
    repo = store.get_active_repository(store.get_project(CONV)["project_id"])
    assert repo["status"] == store.REPO_STRUCTURE_REVIEW and repo["structure"] == STRUCTURE


def test_tools_without_conversation_context_fail_cleanly(ado):
    t = tools(conversation_id=None)
    assert call(t, "save_repository_structure", files=STRUCTURE)["status"] == "error"
    assert call(t, "push_repository_structure")["status"] == "error"


def test_agent_registers_the_five_workflow_tools():
    from agent_framework.openai import OpenAIChatCompletionClient
    client = OpenAIChatCompletionClient(model="test", api_key="k", azure_endpoint="https://example.openai.azure.com",
                                        api_version="2024-12-01-preview")
    agent = rsa.get_repo_setup_agent(client, CONV)
    assert [t.name for t in agent.default_options["tools"]] == [
        "create_ado_project", "create_ado_repository", "save_repository_structure",
        "update_repository_structure", "push_repository_structure"]
    instructions = agent.default_options["instructions"]
    for op in ["GENERATE_PROJECT_PROPOSAL", "UPDATE_PROJECT_PROPOSAL", "CREATE_PROJECT", "GENERATE_REPOSITORY_PROPOSAL",
               "UPDATE_REPOSITORY_PROPOSAL", "CREATE_REPOSITORY", "GENERATE_REPOSITORY_STRUCTURE",
               "UPDATE_REPOSITORY_STRUCTURE", "PUSH_REPOSITORY_STRUCTURE"]:
        assert op in instructions
    assert "template" not in instructions.lower()
    push_schema = {t.name: t for t in agent.default_options["tools"]}["push_repository_structure"].parameters()
    assert "files" not in push_schema["properties"]                       # the model cannot choose what is pushed


# ---------------------------------------------------------------------------
# Orchestrator helpers (conversation_service)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ('{"status": "COMPLETE"}', {"status": "COMPLETE"}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ("not json", None),
    ("", None),
])
def test_parse_agent_json(text, expected):
    assert cs.parse_agent_json(text) == expected


def test_persist_requirements_output_saves_prd_and_trd(db):
    cs.persist_requirements_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PRD",
                                                     "structured_project_response": {"Objective": "o"}}))
    cs.persist_requirements_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "TRD",
                                                     "technical_requirements_document": TRD}))
    cs.persist_requirements_output(CONV, json.dumps({"status": "CLARIFICATION_REQUIRED", "questions": ["?"]}))
    project = store.get_project(CONV)
    assert project["prd"] == {"Objective": "o"} and project["trd"] == TRD


def test_persist_repo_setup_output_saves_proposals(db):
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PROJECT_REVIEW",
                                                   "project_proposal": {"project_name": "Svc", "project_description": "Go"}}))
    project = store.get_project(CONV)
    assert (project["project_name"], project["workflow_stage"]) == ("Svc", store.STAGE_PROJECT_REVIEW)

    # A repository name proposal is only stored once the ADO project exists
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "REPOSITORY_REVIEW",
                                                   "repository_proposal": {"repo_name": "svc"}}))
    assert store.get_active_repository(project["project_id"]) is None
    store.update_project(project["project_id"], ado_project_id="proj-guid")
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PROJECT_CREATED",
                                                   "repository_proposal": {"repo_name": "svc"}}))
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "REPOSITORY_REVIEW",
                                                   "repository_proposal": {"repo_name": "svc-api"}}))
    assert store.get_active_repository(project["project_id"])["repo_name"] == "svc-api"
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_REPOSITORY_REVIEW

    # Proposals never overwrite a created project
    cs.persist_repo_setup_output(CONV, json.dumps({"status": "COMPLETE", "workflow_stage": "PROJECT_REVIEW",
                                                   "project_proposal": {"project_name": "Other"}}))
    assert store.get_project(CONV)["project_name"] == "Svc"


def test_insert_structure_tree():
    tree = "```text\n/\n└── a.txt\n```"
    assert cs.insert_structure_tree(f"Intro\n{cs.STRUCTURE_PLACEHOLDER}\nOutro", tree) == f"Intro\n{tree}\nOutro"
    assert cs.insert_structure_tree("No placeholder", tree) == f"No placeholder\n\n{tree}"
    assert cs.insert_structure_tree(f"x {cs.STRUCTURE_PLACEHOLDER}", None) == "x "
    doubled = cs.insert_structure_tree(f"{cs.STRUCTURE_PLACEHOLDER}\n{cs.STRUCTURE_PLACEHOLDER}", tree)
    assert doubled.count("a.txt") == 1


def test_render_structure_for_display_marks_pushed(db):
    project = store.ensure_project(CONV)
    repo = store.propose_repository_name(project["project_id"], "svc")
    assert cs.render_structure_for_display(store.get_workflow_state(CONV)) is None
    store.save_structure(repo["repo_id"], STRUCTURE, 0)
    assert cs.render_structure_for_display(store.get_workflow_state(CONV)).startswith("**Proposed repository structure** (version 1, 3 files)")
    store.update_repository(repo["repo_id"], status=store.REPO_PUSHED)
    assert cs.render_structure_for_display(store.get_workflow_state(CONV)).startswith("**Pushed repository structure**")
