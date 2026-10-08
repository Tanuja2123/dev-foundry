import base64
import json

import httpx
import pytest

from app.services.mcp import ado_git_push as gp
from app.services.mcp.ado_git_push import AdoPushError, ZERO_OBJECT_ID, push_files_to_repo
from tests.fake_ado import FakeAdo, ORG_URL, PAT

pytestmark = pytest.mark.usefixtures("no_real_network")

FILES = {"/README.md": "# Repo\n", "backend/app/__init__.py": '"""pkg"""\n'}


def push(fake: FakeAdo, files=None, sleeps=None, **kwargs):
    sleeps = sleeps if sleeps is not None else []
    return push_files_to_repo(kwargs.pop("project", "Proj"), kwargs.pop("repository", "repo"), files or FILES,
                              org_url=ORG_URL, pat=PAT, client=fake.client(), sleep=sleeps.append, **kwargs)


def push_bodies(fake: FakeAdo):
    return [json.loads(r.content) for r in fake.posts()]


@pytest.fixture(autouse=True)
def _clear_scaffold_env(monkeypatch):
    monkeypatch.delenv("ADO_SCAFFOLD_COMMIT_MESSAGE", raising=False)
    monkeypatch.delenv("ADO_SCAFFOLD_DEFAULT_BRANCH", raising=False)


# ---------------------------------------------------------------------------
# Path / branch / file validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("README.md", "/README.md"),
    ("/a/b/c.txt", "/a/b/c.txt"),
    ("a\\b\\c.py", "/a/b/c.py"),
    ("//a///b.txt", "/a/b.txt"),
    ("  /docs/x.md  ", "/docs/x.md"),
    ("frontend/src/components/.gitkeep", "/frontend/src/components/.gitkeep"),
])
def test_normalize_path_valid(raw, expected):
    assert gp.normalize_path(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "/", "a/../b.txt", "../secret", "./a.txt", "a/./b.txt", "docs/", "a\\",
    ".git/config", "src/.GIT/hooks/x", "a\tb.txt", "/" + "a" * 500,
])
def test_normalize_path_rejects_invalid(raw):
    with pytest.raises(AdoPushError) as err:
        gp.normalize_path(raw)
    assert err.value.error_type == "VALIDATION_ERROR"


@pytest.mark.parametrize("raw, expected", [("main", "main"), ("refs/heads/develop", "develop"),
                                           (" feature/x ", "feature/x")])
def test_normalize_branch_valid(raw, expected):
    assert gp.normalize_branch(raw) == expected


@pytest.mark.parametrize("raw", ["", "bad name", "a..b", "x.lock", "feat/", "/x", "a:b", "refs/heads/"])
def test_normalize_branch_rejects_invalid(raw):
    with pytest.raises(AdoPushError):
        gp.normalize_branch(raw)


def test_prepare_files_detects_duplicates_after_normalization():
    with pytest.raises(AdoPushError, match="Duplicate"):
        gp.prepare_files({"a.txt": "x", "/a.txt": "y"})


def test_prepare_files_detects_case_insensitive_duplicates():
    with pytest.raises(AdoPushError, match="Duplicate"):
        gp.prepare_files({"/Docs/README.md": "x", "/docs/readme.md": "y"})


@pytest.mark.parametrize("content", ["", "   ", "\n\t\n", None, 123])
def test_prepare_files_rejects_empty_or_non_text_content(content):
    with pytest.raises(AdoPushError, match="empty content"):
        gp.prepare_files({"/a.txt": content})


def test_prepare_files_limits():
    with pytest.raises(AdoPushError, match="At least one file"):
        gp.prepare_files({})
    with pytest.raises(AdoPushError, match="Too many files"):
        gp.prepare_files({f"/f{i}.txt": "x" for i in range(4)}, max_files=3)
    with pytest.raises(AdoPushError, match="limit is 10 bytes"):
        gp.prepare_files({"/big.txt": "x" * 11}, max_file_bytes=10)
    with pytest.raises(AdoPushError, match="Total content"):
        gp.prepare_files({"/a.txt": "x" * 6, "/b.txt": "x" * 6}, max_total_bytes=10)


def test_prepare_files_normalizes_keys():
    assert gp.prepare_files({"a\\b.txt": "x"}) == {"/a/b.txt": "x"}


# ---------------------------------------------------------------------------
# Request body shape
# ---------------------------------------------------------------------------

def test_build_push_body_exact_shape():
    body = gp.build_push_body("main", ZERO_OBJECT_ID, "msg", {"/a.txt": "A", "/b/c.py": "C"})
    assert body == {
        "refUpdates": [{"name": "refs/heads/main", "oldObjectId": "0" * 40}],
        "commits": [{
            "comment": "msg",
            "changes": [
                {"changeType": "add", "item": {"path": "/a.txt"}, "newContent": {"content": "A", "contentType": "rawtext"}},
                {"changeType": "add", "item": {"path": "/b/c.py"}, "newContent": {"content": "C", "contentType": "rawtext"}},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# Push behaviour
# ---------------------------------------------------------------------------

def test_push_to_empty_repo_uses_zero_sha_single_commit_and_default_message():
    fake = FakeAdo()
    result = push(fake)

    bodies = push_bodies(fake)
    assert len(bodies) == 1                                   # one push
    assert len(bodies[0]["commits"]) == 1                     # one commit
    assert bodies[0]["refUpdates"] == [{"name": "refs/heads/main", "oldObjectId": ZERO_OBJECT_ID}]
    assert bodies[0]["commits"][0]["comment"] == "chore: initial project scaffold"
    assert [c["item"]["path"] for c in bodies[0]["commits"][0]["changes"]] == ["/README.md", "/backend/app/__init__.py"]

    assert result["status"] == "success"
    assert result["branch"] == "main"
    assert result["created_branch"] is True
    assert result["commit_id"] == fake.branches["main"]
    assert result["files_count"] == 2
    assert result["files"] == ["/README.md", "/backend/app/__init__.py"]
    assert result["push_url"].endswith("/pushes/1")
    assert result["commit_url"] == f"{ORG_URL}/Proj/_git/repo/commit/{result['commit_id']}"
    assert result["commit_message"] == "chore: initial project scaffold"


def test_push_sends_basic_auth_with_pat_and_correct_urls():
    fake = FakeAdo()
    push(fake, project="My Project", repository="my repo")
    expected = "Basic " + base64.b64encode(f":{PAT}".encode()).decode()
    assert all(r.headers["Authorization"] == expected for r in fake.requests)
    post = fake.posts()[0]
    assert post.url.raw_path.decode().startswith("/fake-org/My%20Project/_apis/git/repositories/my%20repo/pushes")
    assert post.url.params["api-version"] == "7.1"


def test_custom_commit_message_and_whitespace_falls_back_to_default():
    fake = FakeAdo()
    assert push(fake, commit_message="  Initial scaffold  ")["commit_message"] == "Initial scaffold"
    fake2 = FakeAdo()
    assert push(fake2, commit_message="   ")["commit_message"] == "chore: initial project scaffold"


def test_default_commit_message_and_branch_from_env(monkeypatch):
    monkeypatch.setenv("ADO_SCAFFOLD_COMMIT_MESSAGE", "chore: scaffold from env")
    monkeypatch.setenv("ADO_SCAFFOLD_DEFAULT_BRANCH", "trunk")
    fake = FakeAdo()
    result = push(fake)
    assert result["commit_message"] == "chore: scaffold from env"
    assert result["branch"] == "trunk"
    assert push_bodies(fake)[0]["refUpdates"][0]["name"] == "refs/heads/trunk"


def test_existing_branch_uses_head_sha_when_allowed():
    fake = FakeAdo(branches={"main": "a" * 40}, default_branch="main")
    result = push(fake, allow_existing_branch=True)
    assert push_bodies(fake)[0]["refUpdates"][0]["oldObjectId"] == "a" * 40
    assert result["created_branch"] is False


def test_existing_branch_rejected_without_pushing_when_not_allowed():
    fake = FakeAdo(branches={"main": "a" * 40}, default_branch="main")
    with pytest.raises(AdoPushError) as err:
        push(fake, allow_existing_branch=False)
    assert err.value.error_type == "ALREADY_INITIALIZED"
    assert fake.posts() == []                                 # nothing pushed, nothing half-created


def test_default_branch_not_main_is_respected():
    fake = FakeAdo(branches={"develop": "b" * 40}, default_branch="develop")
    result = push(fake, allow_existing_branch=True)
    assert result["branch"] == "develop"
    assert push_bodies(fake)[0]["refUpdates"][0] == {"name": "refs/heads/develop", "oldObjectId": "b" * 40}


def test_explicit_branch_overrides_default_and_creates_new_branch():
    fake = FakeAdo(branches={"main": "a" * 40}, default_branch="main")
    result = push(fake, branch="refs/heads/scaffold", allow_existing_branch=False)
    assert result["branch"] == "scaffold"
    assert push_bodies(fake)[0]["refUpdates"][0] == {"name": "refs/heads/scaffold", "oldObjectId": ZERO_OBJECT_ID}


def test_refs_prefix_match_is_not_mistaken_for_target_branch():
    # Real ADO returns refs/heads/mainx for filter=heads/main (prefix match)
    fake = FakeAdo(branches={"mainx": "c" * 40}, default_branch="mainx")
    push(fake, branch="main", allow_existing_branch=False)
    assert push_bodies(fake)[0]["refUpdates"][0]["oldObjectId"] == ZERO_OBJECT_ID


def test_stale_head_is_refetched_and_retried_once():
    fake = FakeAdo(branches={"main": "a" * 40}, default_branch="main")
    calls = {"n": 0}

    def move_branch_before_first_push(request, f):
        if request.method == "POST" and calls["n"] == 0:
            calls["n"] += 1
            f.branches["main"] = "d" * 40                   # someone else pushed meanwhile
        return None

    fake.override = move_branch_before_first_push
    result = push(fake, allow_existing_branch=True)
    olds = [b["refUpdates"][0]["oldObjectId"] for b in push_bodies(fake)]
    assert olds == ["a" * 40, "d" * 40]
    assert result["status"] == "success"


def test_stale_head_twice_raises_conflict():
    fake = FakeAdo(branches={"main": "a" * 40}, default_branch="main")

    def always_move(request, f):
        if request.method == "POST":
            f.branches["main"] = format(len(f.requests), "040x")
        return None

    fake.override = always_move
    with pytest.raises(AdoPushError) as err:
        push(fake, allow_existing_branch=True)
    assert err.value.error_type == "CONFLICT"
    assert err.value.status_code == 409
    assert "TF401028" in err.value.details
    assert len(fake.posts()) == 2


def test_branch_created_concurrently_on_empty_repo_fails_cleanly():
    fake = FakeAdo()

    def create_branch_first(request, f):
        if request.method == "POST" and not f.branches:
            f.branches["main"] = "e" * 40
        return None

    fake.override = create_branch_first
    with pytest.raises(AdoPushError) as err:
        push(fake, allow_existing_branch=False)
    assert err.value.error_type == "ALREADY_INITIALIZED"
    assert len(fake.posts()) == 1


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_retries_transient_get_failure():
    fake = FakeAdo()
    state = {"failed": False}

    def fail_first_get(request, f):
        if request.method == "GET" and not state["failed"]:
            state["failed"] = True
            return httpx.Response(503, text="Service Unavailable")
        return None

    fake.override = fail_first_get
    sleeps = []
    result = push(fake, sleeps=sleeps)
    assert result["status"] == "success"
    assert sleeps == [1.0]


def test_retry_after_header_is_honored_and_capped():
    fake = FakeAdo()
    state = {"n": 0}

    def throttle_post(request, f):
        if request.method == "POST" and state["n"] < 2:
            state["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "2" if state["n"] == 1 else "120"})
        return None

    fake.override = throttle_post
    sleeps = []
    result = push(fake, sleeps=sleeps)
    assert result["status"] == "success"
    assert sleeps == [2.0, gp.MAX_RETRY_AFTER_SECONDS]
    assert len(fake.posts()) == 3


def test_retries_exhausted_raises_with_server_body():
    fake = FakeAdo()
    fake.override = lambda request, f: httpx.Response(503, text="backend overloaded") if request.method == "POST" else None
    with pytest.raises(AdoPushError) as err:
        push(fake)
    assert err.value.status_code == 503
    assert "backend overloaded" in err.value.details
    assert len(fake.posts()) == 1 + gp.MAX_RETRIES


def test_network_error_retried_then_raised():
    fake = FakeAdo()

    def boom(request, f):
        raise httpx.ConnectTimeout("timed out", request=request)

    fake.override = boom
    with pytest.raises(AdoPushError) as err:
        push(fake)
    assert err.value.error_type == "NETWORK_ERROR"
    assert len(fake.requests) == 1 + gp.MAX_RETRIES


def test_push_applied_despite_5xx_is_detected_as_success():
    fake = FakeAdo()
    state = {"n": 0}

    def apply_then_fail(request, f):
        if request.method == "POST" and state["n"] == 0:
            state["n"] += 1
            f.apply_push(json.loads(request.content))      # server applied it ...
            return httpx.Response(502, text="Bad Gateway")  # ... but the response was lost
        return None

    fake.override = apply_then_fail
    result = push(fake, commit_message="Initial scaffold")
    assert result["status"] == "success"
    assert result["commit_id"] == fake.branches["main"]
    assert len(fake.pushes) == 1                          # applied exactly once


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, body, ctype, expected", [
    (401, "Unauthorized", "text/plain", "AUTHENTICATION_FAILED"),
    (302, "<html>Object moved</html>", "text/html", "AUTHENTICATION_FAILED"),   # verified real ADO behaviour
    (203, "<html>Sign in</html>", "text/html", "AUTHENTICATION_FAILED"),
    (403, '{"message":"TF401027: You need the Git GenericContribute permission"}', "application/json", "PERMISSION_DENIED"),
    (404, '{"message":"TF200016: The following project does not exist"}', "application/json", "NOT_FOUND"),
    (400, '{"message":"The path /README.md specified in the add operation already exists."}', "application/json", "BAD_REQUEST"),
])
def test_error_mapping_on_push(status, body, ctype, expected):
    fake = FakeAdo()
    fake.override = lambda request, f: (httpx.Response(status, text=body, headers={"content-type": ctype})
                                        if request.method == "POST" else None)
    with pytest.raises(AdoPushError) as err:
        push(fake)
    assert err.value.error_type == expected
    assert err.value.status_code == status
    if expected != "AUTHENTICATION_FAILED":
        assert json.loads(body)["message"] in err.value.details


def test_missing_repository_maps_to_not_found_before_push():
    fake = FakeAdo(repo_exists=False)
    with pytest.raises(AdoPushError) as err:
        push(fake)
    assert err.value.error_type == "NOT_FOUND"
    assert "TF401019" in err.value.details
    assert fake.posts() == []


def test_pat_is_redacted_from_error_details():
    fake = FakeAdo()
    fake.override = lambda request, f: (httpx.Response(400, text=f"bad request for token {PAT}")
                                        if request.method == "POST" else None)
    with pytest.raises(AdoPushError) as err:
        push(fake)
    assert PAT not in err.value.details
    assert "[REDACTED_PAT]" in err.value.details
    assert PAT not in json.dumps(err.value.to_dict())


def test_missing_configuration(no_ado_config):
    with pytest.raises(AdoPushError) as err:
        push_files_to_repo("Proj", "repo", FILES)
    assert err.value.error_type == "CONFIGURATION_ERROR"


def test_validation_happens_before_any_request():
    fake = FakeAdo()
    with pytest.raises(AdoPushError):
        push(fake, files={"/ok.txt": "x", "/bad/../x.txt": "y"})
    with pytest.raises(AdoPushError):
        push(fake, files={"/ok.txt": "x", "/empty.txt": ""})
    with pytest.raises(AdoPushError):
        push(fake, repository="  ")
    assert fake.requests == []
