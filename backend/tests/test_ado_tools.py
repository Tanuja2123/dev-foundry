import json

import httpx
import pytest

from app.services.mcp import ado_git_push as gp
from app.services.mcp import ado_mcp_server as server

pytestmark = pytest.mark.usefixtures("no_real_network")


@pytest.fixture
def ado_env(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_ORG_NAME", "fake-org")
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "fake-pat")


@pytest.fixture
def captured_push(monkeypatch):
    calls = []

    def fake_push(project, repository, files, commit_message=None, branch=None, **kwargs):
        calls.append({"project": project, "repository": repository, "files": dict(files),
                      "commit_message": commit_message, "branch": branch, **kwargs})
        return {"status": "success", "commit_id": "abc123", "files": sorted(files), "files_count": len(files)}

    monkeypatch.setattr(gp, "push_files_to_repo", fake_push)
    return calls


# ---------------------------------------------------------------------------
# push_repository_files MCP tool
# ---------------------------------------------------------------------------

def test_push_repository_files_accepts_dicts_and_models(captured_push):
    class Model:
        def model_dump(self):
            return {"path": "b/.gitkeep", "content": "keep\n"}

    result = server.push_repository_files("Proj", "repo", [{"path": "/a/README.md", "content": "# A\n"}, Model()],
                                          branch="develop")
    assert result["status"] == "success"
    assert captured_push[0]["files"] == {"/a/README.md": "# A\n", "b/.gitkeep": "keep\n"}
    assert captured_push[0]["branch"] == "develop"
    assert captured_push[0]["allow_existing_branch"] is False


@pytest.mark.parametrize("files, message", [
    ([{"path": "/a.txt"}], "must have string 'path' and 'content'"),
    ([{"path": "/a.txt", "content": "x"}, {"path": "/a.txt", "content": "y"}], "Duplicate"),
])
def test_push_repository_files_rejects_malformed_input(captured_push, files, message):
    result = server.push_repository_files("Proj", "repo", files)
    assert result["status"] == "error"
    assert message in result["message"]
    assert captured_push == []


def test_push_repository_files_maps_push_error(monkeypatch):
    def failing(*args, **kwargs):
        raise gp.AdoPushError("Branch 'main' already has commits", error_type="ALREADY_INITIALIZED", status_code=409)

    monkeypatch.setattr(gp, "push_files_to_repo", failing)
    result = server.push_repository_files("Proj", "repo", [{"path": "/a.txt", "content": "a"}])
    assert result == {"status": "error", "error_type": "ALREADY_INITIALIZED", "status_code": 409,
                      "message": "Branch 'main' already has commits", "details": ""}


def test_push_repository_scaffold_keeps_pr14_contract(captured_push):
    result = server.push_repository_scaffold("Proj", "repo", [
        {"path": "docs/", "content": ""},                 # folder -> .gitkeep with placeholder
        {"path": "README.md", "content": "# Repo"},
        {"path": "/README.md", "content": "duplicate"},   # duplicate -> first one wins
        {"path": "src/empty.py", "content": None},       # empty file -> placeholder
        "not-a-dict",
    ])
    call = captured_push[0]
    assert call["files"] == {"/docs/.gitkeep": server.SCAFFOLD_PLACEHOLDER, "/README.md": "# Repo",
                             "/src/empty.py": "# empty.py\n"}
    assert (call["branch"], call["allow_existing_branch"]) == ("main", True)
    assert call["commit_message"] == "Initial commit: Project structure scaffolding"
    assert result["status"] == "success" and result["repository"] == "repo"
    assert result["pushed_files_count"] == 3 and "commit_id" in result


def test_push_repository_scaffold_without_files_adds_readme(captured_push):
    server.push_repository_scaffold("Proj", "repo", [])
    assert list(captured_push[0]["files"]) == ["/README.md"]


def test_push_repository_files_validation_happens_without_network(ado_env, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(httpx.Client, "request", no_network)
    assert server.push_repository_files("Proj", "repo", [{"path": "/../etc/passwd", "content": "x"}])["error_type"] == "VALIDATION_ERROR"
    assert server.push_repository_files("Proj", "repo", [{"path": "/empty.txt", "content": "  "}])["error_type"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# create_ado_project (waits for the queued operation, returns the real project id)
# create_ado_repository (unchanged)
# ---------------------------------------------------------------------------

_REAL_HTTPX_CLIENT = httpx.Client


class _ScriptedClient:
    """Stands in for httpx.Client inside ado_mcp_server; answers from a per-test script."""
    requests = []
    operation_statuses = []
    project_status_code = 200

    repos = None

    def __init__(self, *args, **kwargs):
        self._client = _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self._handle))

    @classmethod
    def reset(cls, operation_statuses, project_status_code=200, repos=None):
        cls.requests, cls.operation_statuses, cls.project_status_code = [], list(operation_statuses), project_status_code
        cls.repos = list(repos) if repos is not None else None

    @classmethod
    def _handle(cls, request):
        cls.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/_apis/projects"):
            return httpx.Response(202, json={"id": "op-1", "status": "queued",
                                             "url": "https://dev.azure.com/fake-org/_apis/operations/op-1"})
        if "/_apis/operations/" in path:
            status = cls.operation_statuses.pop(0) if cls.operation_statuses else "inProgress"
            body = {"id": "op-1", "status": status}
            if status == "failed":
                body["detailedMessage"] = "TF200019: The project name is already in use."
            return httpx.Response(200, json=body)
        if request.method == "GET" and "/_apis/projects/" in path:
            if cls.project_status_code != 200:
                return httpx.Response(cls.project_status_code, text="not found")
            return httpx.Response(200, json={"id": "real-project-guid", "name": "My Proj", "state": "wellFormed",
                                             "_links": {"web": {"href": "https://dev.azure.com/fake-org/My%20Proj"}}})
        if request.method == "PATCH" and "/_apis/projects/" in path:
            return httpx.Response(202, json={"id": "op-1", "status": "notSet"})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            if cls.repos is not None:
                return httpx.Response(200, json={"value": cls.repos})
            return httpx.Response(404, text="not found")
        if request.method == "PATCH" and "/_apis/git/repositories/" in path:
            body = json.loads(request.content)
            new_name = body.get("name")
            repo_id = path.rstrip("/").split("/")[-1]
            return httpx.Response(200, json={"id": repo_id, "name": new_name,
                                             "webUrl": f"https://dev.azure.com/fake-org/Proj/_git/{new_name}"})
        if request.method == "POST" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(201, json={"id": "repo-1", "webUrl": "https://dev.azure.com/fake-org/Proj/_git/repo"})
        return httpx.Response(400, text="unexpected")

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        self._client.close()


@pytest.fixture
def scripted(ado_env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(server.httpx, "Client", _ScriptedClient)
    monkeypatch.setattr(server, "_sleep", sleeps.append)
    return sleeps


def test_create_ado_project_returns_real_project_id_after_operation_succeeds(scripted):
    _ScriptedClient.reset(["queued", "inProgress", "succeeded"])
    result = server.create_ado_project("My Proj", "Desc")
    assert result == {"status": "success", "name": "My Proj", "description": "Desc",
                      "id": "real-project-guid", "url": "https://dev.azure.com/fake-org/My%20Proj"}
    assert result["id"] != "op-1"                                          # not the operation id
    urls = [str(r.url) for r in _ScriptedClient.requests]
    assert urls[1].startswith("https://dev.azure.com/fake-org/_apis/operations/op-1")
    assert urls[-1].startswith("https://dev.azure.com/fake-org/_apis/projects/My%20Proj")
    assert scripted == [server.PROJECT_POLL_INTERVAL_SECONDS] * 2
    body = json.loads(_ScriptedClient.requests[0].content)
    assert body["name"] == "My Proj" and body["capabilities"]["versioncontrol"]["sourceControlType"] == "Git"


def test_create_ado_project_reports_failed_operation(scripted):
    _ScriptedClient.reset(["failed"])
    result = server.create_ado_project("My Proj", "Desc")
    assert result["status"] == "error"
    assert "failed" in result["message"] and "TF200019" in result["details"]


def test_create_ado_project_times_out_cleanly(scripted, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_CREATION_TIMEOUT_SECONDS", 0)
    _ScriptedClient.reset([])
    result = server.create_ado_project("My Proj", "Desc")
    assert result["status"] == "error" and result["error_type"] == "PROVISIONING_TIMEOUT"


def test_create_ado_project_reports_unreadable_project(scripted):
    _ScriptedClient.reset(["succeeded"], project_status_code=404)
    result = server.create_ado_project("My Proj", "Desc")
    assert result["status"] == "error" and "could not be read back" in result["message"]


def test_update_ado_project_waits_for_operation_and_returns_project(scripted):
    _ScriptedClient.reset(["inProgress", "succeeded"])
    result = server.update_ado_project("real-project-guid", project_name="My Proj", project_description="New desc")
    assert result["status"] == "success"
    assert (result["id"], result["name"], result["url"]) == ("real-project-guid", "My Proj", "https://dev.azure.com/fake-org/My%20Proj")
    patch = _ScriptedClient.requests[0]
    assert patch.method == "PATCH" and patch.url.path.endswith("/_apis/projects/real-project-guid")
    assert json.loads(patch.content) == {"name": "My Proj", "description": "New desc"}


def test_update_ado_project_validation_and_failure(scripted):
    _ScriptedClient.reset([])
    assert server.update_ado_project("id", project_name="  ", project_description=None)["error_type"] == "VALIDATION_ERROR"
    assert _ScriptedClient.requests == []
    _ScriptedClient.reset(["failed"])
    failed = server.update_ado_project("id", project_name="Taken name")
    assert failed["status"] == "error" and "TF200019" in failed["details"]


def test_push_repository_changes_passes_base_files(captured_push):
    result = server.push_repository_changes("Proj", "repo", base_files=[{"path": "/a.txt", "content": "a"}],
                                            files=[{"path": "/a.txt", "content": "A"}], commit_message="update")
    assert result["status"] == "success"
    assert captured_push[0]["base_files"] == {"/a.txt": "a"} and captured_push[0]["files"] == {"/a.txt": "A"}


def test_create_ado_repository_unchanged(scripted):
    _ScriptedClient.reset([])
    result = server.create_ado_repository("Proj", "repo")
    assert result == {"status": "success", "name": "repo", "id": "repo-1",
                      "url": "https://dev.azure.com/fake-org/Proj/_git/repo"}


def test_create_ado_repository_renames_empty_default_repo(scripted):
    # When Azure DevOps created an empty default repo (defaultBranch is None)
    _ScriptedClient.reset([], repos=[{"id": "default-id", "name": "My Proj", "defaultBranch": None}])
    result = server.create_ado_repository("Proj", "my-repo")
    assert result == {"status": "success", "name": "my-repo", "id": "default-id",
                      "url": "https://dev.azure.com/fake-org/Proj/_git/my-repo"}


def test_create_ado_repository_returns_existing_repo(scripted):
    # When the repo already exists with the desired name
    _ScriptedClient.reset([], repos=[{"id": "existing-id", "name": "my-repo", "defaultBranch": "refs/heads/main",
                                      "webUrl": "https://dev.azure.com/fake-org/Proj/_git/my-repo"}])
    result = server.create_ado_repository("Proj", "my-repo")
    assert result == {"status": "success", "name": "my-repo", "id": "existing-id",
                      "url": "https://dev.azure.com/fake-org/Proj/_git/my-repo"}


def test_repository_reuse_ignores_case_and_prefers_the_projects_own_empty_repo(scripted):
    # AGT-25: Azure DevOps repository names ignore case; the project's own default repo (named like the project) is
    # renamed, not some other empty repository the user created
    _ScriptedClient.reset([], repos=[{"id": "existing-id", "name": "My-Repo", "defaultBranch": "refs/heads/main"}])
    assert server.create_ado_repository("Proj", "my-repo")["id"] == "existing-id"
    _ScriptedClient.reset([], repos=[{"id": "other-empty", "name": "scratch", "defaultBranch": None},
                                     {"id": "default-id", "name": "Proj", "defaultBranch": None}])
    assert server.create_ado_repository("Proj", "my-repo")["id"] == "default-id"
    renames = [r for r in _ScriptedClient.requests if r.method == "PATCH"]
    assert len(renames) == 1 and renames[0].url.path.endswith("/default-id")


def test_existing_tools_report_missing_config(no_ado_config):
    assert server.create_ado_project("P", "D")["status"] == "error"
    assert server.create_ado_repository("P", "r")["status"] == "error"


def test_sanitize_ado_project_name():
    # Replaces & with and
    assert server.sanitize_ado_project_name("Git Changelog & Release Notes CLI") == "Git Changelog and Release Notes CLI"
    assert server.sanitize_ado_project_name("A&B") == "A and B"
    assert server.sanitize_ado_project_name("Foo  &  Bar") == "Foo and Bar"

    # Strips the characters Azure DevOps forbids; keeps the allowed ones ('!', '_', '.', '(', letters of any language)
    assert server.sanitize_ado_project_name("My Project #1 @ 2026! / test?") == "My Project 1 2026! test"
    assert server.sanitize_ado_project_name("Café Ops_v2.0 (beta)") == "Café Ops_v2.0 (beta)"
    assert server.sanitize_ado_project_name("_hidden.") == "hidden"            # no leading '_', no trailing '.'
    assert server.sanitize_ado_project_name("CON") == "CON project"            # reserved name

    # Trims hyphens, spaces, periods from ends
    assert server.sanitize_ado_project_name(" -..My-Project..- ") == "My-Project"

    # Enforces 64 char limit
    long_name = "A" * 70
    assert len(server.sanitize_ado_project_name(long_name)) <= 64

    # Empty fallback
    assert server.sanitize_ado_project_name("") == "project"


def test_create_and_update_ado_project_sanitizes_ampersand(scripted):
    _ScriptedClient.reset(["succeeded"])
    res_create = server.create_ado_project("Git Changelog & Release Notes CLI", "CLI tools")
    assert res_create["status"] == "success"
    assert res_create["name"] == "Git Changelog and Release Notes CLI"
    create_payload = json.loads(_ScriptedClient.requests[0].content)
    assert create_payload["name"] == "Git Changelog and Release Notes CLI"

    _ScriptedClient.reset(["succeeded"])
    res_update = server.update_ado_project("real-project-guid", project_name="Tools & Utilities")
    assert res_update["status"] == "success"
    patch_payload = json.loads(_ScriptedClient.requests[0].content)
    assert patch_payload["name"] == "Tools and Utilities"
