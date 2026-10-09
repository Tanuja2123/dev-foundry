"""In-memory fake of the Azure DevOps Git REST endpoints used by ado_git_push (no network)."""
import hashlib
import json
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

import httpx

ORG_URL = "https://dev.azure.com/fake-org"
PAT = "fake-pat-value-1234567890"


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"content-type": "application/json; charset=utf-8"})


class FakeAdo:
    """
    Behaves like the real service for the cases verified against Azure DevOps:
    - empty repo: no defaultBranch; first push sets it
    - refs?filter= is a prefix match
    - stale/zero oldObjectId on an existing branch -> 409 GitReferenceStaleException
    """

    def __init__(self, branches: Optional[Dict[str, str]] = None, default_branch: Optional[str] = None,
                 repo_exists: bool = True):
        self.branches: Dict[str, str] = dict(branches or {})
        self.default_branch = default_branch
        self.repo_exists = repo_exists
        self.requests: List[httpx.Request] = []
        self.pushes: List[dict] = []
        self.commit_comments: Dict[str, str] = {}
        # Optional hook: return a Response to override the default handling of a request.
        self.override: Optional[Callable[[httpx.Request, "FakeAdo"], Optional[httpx.Response]]] = None

    # -- helpers ------------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def posts(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def apply_push(self, body: dict) -> dict:
        ref = body["refUpdates"][0]
        name = ref["name"].replace("refs/heads/", "")
        comment = body["commits"][0]["comment"]
        commit_id = hashlib.sha1(json.dumps(body, sort_keys=True).encode() + str(len(self.pushes)).encode()).hexdigest()
        self.branches[name] = commit_id
        self.commit_comments[commit_id] = comment
        if self.default_branch is None:
            self.default_branch = name
        self.pushes.append(body)
        return {
            "pushId": len(self.pushes),
            "url": f"{ORG_URL}/_apis/git/repositories/repo-id/pushes/{len(self.pushes)}",
            "commits": [{"commitId": commit_id, "comment": comment}],
            "refUpdates": [{"name": ref["name"], "oldObjectId": ref["oldObjectId"], "newObjectId": commit_id}],
        }

    # -- request handler ----------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override:
            response = self.override(request, self)
            if response is not None:
                return response

        path = unquote(request.url.path)
        if not self.repo_exists:
            return _json(404, {"typeKey": "GitRepositoryNotFoundException",
                               "message": "TF401019: The Git repository with name or identifier repo does not exist."})

        if request.method == "GET" and path.endswith("/refs"):
            prefix = "refs/" + request.url.params.get("filter", "")
            refs = [{"name": f"refs/heads/{b}", "objectId": sha} for b, sha in self.branches.items()
                    if f"refs/heads/{b}".startswith(prefix)]
            return _json(200, {"value": refs, "count": len(refs)})

        if request.method == "GET" and "/commits/" in path:
            commit_id = path.rsplit("/", 1)[-1]
            if commit_id in self.commit_comments:
                return _json(200, {"commitId": commit_id, "comment": self.commit_comments[commit_id]})
            return _json(404, {"message": "commit not found"})

        if request.method == "GET" and "/_apis/git/repositories/" in path:
            body = {"id": "repo-id", "name": path.rsplit("/", 1)[-1], "size": 0}
            if self.default_branch:
                body["defaultBranch"] = f"refs/heads/{self.default_branch}"
            return _json(200, body)

        if request.method == "POST" and path.endswith("/pushes"):
            body = json.loads(request.content)
            ref = body["refUpdates"][0]
            name = ref["name"].replace("refs/heads/", "")
            current = self.branches.get(name, "0" * 40)
            if ref["oldObjectId"] != current:
                return _json(409, {"typeKey": "GitReferenceStaleException",
                                   "message": f"TF401028: The reference '{ref['name']}' has already been updated by another client."})
            return _json(201, self.apply_push(body))

        return _json(400, {"message": f"Unexpected request {request.method} {path}"})
