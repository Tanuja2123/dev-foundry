"""
Opt-in integration tests against a real Azure DevOps organization (and Supabase for the store test).

Run (from backend/, venv active):
    $env:ADO_INTEGRATION_TESTS="1"; $env:ADO_IT_PROJECT="<throwaway project name>"
    pytest -m integration -s

Every repository test creates its own fresh repository (prefixed "it-scaffold-") inside ADO_IT_PROJECT and deletes it.
test_create_ado_project_returns_real_project_id creates and deletes a "DevFoundry-IT-<id>" project.
test_project_store_round_trip needs the Project/Repository tables (db/migrations/001_*.sql) and deletes its rows.
Never point ADO_IT_PROJECT at a real project.
"""
import os
import shutil

import pytest

from tests import ado_live as live

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("ADO_INTEGRATION_TESTS") != "1" or not os.getenv("ADO_IT_PROJECT"),
                       reason="Set ADO_INTEGRATION_TESTS=1 and ADO_IT_PROJECT=<throwaway project> to run"),
]

from app.services import project_store as store  # noqa: E402
from app.services.mcp import ado_mcp_server as server  # noqa: E402
from app.services.mcp.ado_git_push import AdoPushError, push_files_to_repo  # noqa: E402
from app.services.repo_structure import normalize_structure  # noqa: E402

PROJECT = os.getenv("ADO_IT_PROJECT", "")

STRUCTURE = normalize_structure([
    {"path": "/README.md", "content": "# it-scaffold\n\nIntegration test repository.\n"},
    {"path": "/.gitignore", "content": "target/\n.env\n"},
    {"path": "/cmd/server/main.go", "content": "package main\n\n// Entry point placeholder.\n"},
    {"path": "/internal/handlers/.gitkeep", "content": "# keep\n"},
    {"path": "/internal/models/.gitkeep", "content": "# keep\n"},
    {"path": "/docs/architecture/README.md", "content": "# Architecture\n"},
])


@pytest.fixture
def fresh_repo():
    created = []

    def make() -> str:
        name = live.unique("it-scaffold")
        result = server.create_ado_repository(project_id_or_name=PROJECT, repository_name=name)
        assert result["status"] == "success", result
        created.append(name)
        assert live.get_repo(PROJECT, name).get("defaultBranch") is None      # empty repo, no branches
        return name

    yield make
    for name in created:
        assert name.startswith("it-scaffold-")
        live.delete_repo(PROJECT, name)


def test_push_structure_to_new_empty_repo(fresh_repo):
    repo = fresh_repo()
    message = "Initial scaffold (integration test)"

    result = server.push_repository_files(PROJECT, repo, STRUCTURE, commit_message=message)
    assert result["status"] == "success", result
    assert result["branch"] == "main" and result["created_branch"] is True
    assert result["files_count"] == len(STRUCTURE)

    tree = live.tree_paths(live.list_tree(PROJECT, repo, "main"))
    files = {p for p, is_folder in tree.items() if not is_folder}
    assert files == {f["path"] for f in STRUCTURE}
    assert {p for p, f in tree.items() if f} == live.folders_of(files)
    for f in STRUCTURE:                                                  # byte-for-byte content
        assert live.get_file_content(PROJECT, repo, f["path"], "main") == f["content"]

    commits = live.list_commits(PROJECT, repo, "main")
    assert len(commits) == 1 and commits[0]["commitId"] == result["commit_id"]
    assert commits[0]["comment"].strip() == message
    assert live.get_repo(PROJECT, repo)["defaultBranch"] == "refs/heads/main"
    print(f"\n[evidence] repo={repo} commit={result['commit_id']} message={message!r}")
    print(live.render_tree(files))

    again = server.push_repository_files(PROJECT, repo, STRUCTURE, commit_message="second")
    assert again["status"] == "error" and again["error_type"] == "ALREADY_INITIALIZED", again
    assert len(live.list_commits(PROJECT, repo, "main")) == 1


def test_core_push_appends_to_existing_branch_from_head(fresh_repo):
    repo = fresh_repo()
    first = push_files_to_repo(PROJECT, repo, {"/a.txt": "a\n"}, commit_message="first")
    second = push_files_to_repo(PROJECT, repo, {"/b.txt": "b\n"}, commit_message="second")
    assert second["created_branch"] is False
    commits = live.list_commits(PROJECT, repo, "main")
    assert [c["commitId"] for c in commits] == [second["commit_id"], first["commit_id"]]


def test_bad_paths_are_rejected_before_touching_the_repo(fresh_repo):
    repo = fresh_repo()
    result = server.push_repository_files(PROJECT, repo, [{"path": "/ok.txt", "content": "ok"},
                                                         {"path": "../escape.txt", "content": "x"}])
    assert result["error_type"] == "VALIDATION_ERROR"
    assert live.get_repo(PROJECT, repo).get("defaultBranch") is None


def test_real_error_mapping_not_found_and_bad_pat():
    with pytest.raises(AdoPushError) as err:
        push_files_to_repo(PROJECT, live.unique("it-scaffold-missing"), {"/a.txt": "a"})
    assert err.value.error_type == "NOT_FOUND" and "TF401019" in err.value.details
    with pytest.raises(AdoPushError) as err:
        push_files_to_repo(PROJECT, "anything", {"/a.txt": "a"}, org_url=live.org_url(), pat="not-a-real-pat")
    assert err.value.error_type == "AUTHENTICATION_FAILED"


def test_create_ado_project_returns_real_project_id():
    name = live.unique("DevFoundry-IT")
    result = server.create_ado_project(name, "Integration test project for DevFoundry. Safe to delete.")
    try:
        assert result["status"] == "success", result
        project_id = live.wait_for_project(name, timeout=10)
        assert result["id"] == project_id                                   # the real project id, not the operation id
        assert result["url"].rstrip("/").endswith(name)
        print(f"\n[evidence] project={name} id={result['id']} url={result['url']}")
    finally:
        if result.get("status") == "success":
            assert name.startswith("DevFoundry-IT-")
            print(f"[cleanup] delete project {name}: HTTP {live.delete_project(result['id'])}")


@pytest.mark.skipif(os.getenv("SUPABASE_INTEGRATION_TESTS") != "1",
                    reason="Set SUPABASE_INTEGRATION_TESTS=1 once the Project/Repository tables exist")
def test_project_store_round_trip():
    conv_id = live.unique("it-conv")
    try:
        project = store.ensure_project(conv_id)
        store.update_project(project["project_id"], prd={"Objective": "o"}, trd={"Backend": "Go"},
                             project_name="IT", workflow_stage=store.STAGE_PROJECT_REVIEW)
        repo = store.propose_repository_name(project["project_id"], "it-repo")
        saved = store.save_structure(repo["repo_id"], STRUCTURE, repo["structure_version"])
        state = store.get_workflow_state(conv_id)
        assert state["project"]["trd"] == {"Backend": "Go"}
        assert state["repository"]["structure"] == STRUCTURE                 # jsonb round-trips exactly
        assert saved["structure_version"] == 1 and saved["status"] == store.REPO_STRUCTURE_REVIEW
    finally:
        live.delete_project_rows(conv_id)
    assert store.get_project(conv_id) is None


def test_follow_up_commit_adds_edits_and_deletes(fresh_repo):
    repo = fresh_repo()
    first = server.push_repository_files(PROJECT, repo, STRUCTURE, commit_message="Initial scaffold")
    assert first["status"] == "success", first
    updated = [dict(f) for f in STRUCTURE if f["path"] != "/internal/models/.gitkeep"]       # delete
    for f in updated:
        if f["path"] == "/README.md":
            f["content"] = "# it-scaffold\n\nUpdated after the PRD changed.\n"                 # edit
    updated.append({"path": "/docs/adr/README.md", "content": "# ADRs\n"})                    # add

    result = server.push_repository_changes(PROJECT, repo, base_files=STRUCTURE, files=updated,
                                            commit_message="Update scaffold")
    assert result["status"] == "success", result
    assert result["changes"] == {"add": ["/docs/adr/README.md"], "edit": ["/README.md"],
                                 "delete": ["/internal/models/.gitkeep"]}
    commits = live.list_commits(PROJECT, repo, "main")
    assert [c["comment"].strip() for c in commits] == ["Update scaffold", "Initial scaffold"]
    tree = live.tree_paths(live.list_tree(PROJECT, repo, "main"))
    assert {p for p, f in tree.items() if not f} == {f["path"] for f in updated}
    assert "/internal/models" not in tree                                  # folder disappears with its last file
    assert live.get_file_content(PROJECT, repo, "/README.md", "main").endswith("Updated after the PRD changed.\n")

    again = server.push_repository_changes(PROJECT, repo, base_files=updated, files=updated)
    assert again["error_type"] == "NO_CHANGES"


def test_update_ado_project_renames_and_redescribes():
    name = live.unique("DevFoundry-IT")
    created = server.create_ado_project(name, "original description")
    assert created["status"] == "success", created
    try:
        updated = server.update_ado_project(created["id"], project_name=name + "-v2",
                                            project_description="Updated after the TRD changed")
        assert updated["status"] == "success", updated
        assert (updated["id"], updated["name"], updated["description"]) == (
            created["id"], name + "-v2", "Updated after the TRD changed")
        assert updated["url"].rstrip("/").endswith(name + "-v2")
        print(f"\n[evidence] renamed {name} -> {updated['name']} (id {updated['id']})")
    finally:
        print(f"[cleanup] delete project: HTTP {live.delete_project(created['id'])}")


@pytest.mark.skipif(not (shutil.which("uv") and shutil.which("npx")), reason="needs uv and npx")
def test_real_generator_output_is_pushed_byte_for_byte_then_requested_changes(fresh_repo):
    """React (create-vite) + FastAPI (uv) generated by real commands: empty files, a PNG and a lock file land in ADO
    exactly as generated (commit 1); a requested file follows as commit 2 with only that change."""
    from app.services.repo_structure import apply_structure_update
    from app.services.scaffold_service import decode_file, run_scaffold_commands

    repo = fresh_repo()
    result = run_scaffold_commands([
        {"command": "npx --yes create-vite@latest frontend --template react-ts"},
        {"command": "uv init --app --vcs none --name backend backend"},
        {"command": "uv add --no-sync fastapi[standard]", "cwd": "backend"}])
    scaffold = result.files
    assert any(f.get("encoding") == "base64" for f in scaffold) and any(f["content"] == "" for f in scaffold)

    first = server.push_repository_files(PROJECT, repo, scaffold, commit_message="chore: generator output",
                                         allow_empty=True)
    assert first["status"] == "success", first
    tree = live.tree_paths(live.list_tree(PROJECT, repo, "main"))
    assert {p for p, folder in tree.items() if not folder} == {f["path"] for f in scaffold}
    for f in scaffold:
        assert live.get_file_bytes(PROJECT, repo, f["path"], "main") == decode_file(f), f["path"]

    structure = apply_structure_update(scaffold, upsert_files=[
        {"path": "/backend/app/auth/__init__.py", "content": '"""Authentication."""\n'}])
    second = server.push_repository_changes(PROJECT, repo, base_files=scaffold, files=structure,
                                            commit_message="feat: add auth package", branch="main", allow_empty=True)
    assert second["status"] == "success", second
    assert second["changes"] == {"add": ["/backend/app/auth/__init__.py"], "edit": [], "delete": []}
    commits = live.list_commits(PROJECT, repo, "main")
    assert [c["comment"] for c in commits] == ["feat: add auth package", "chore: generator output"]
