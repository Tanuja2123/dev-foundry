"""
Azure DevOps pull requests for the security review (FEATURES.md SEC-A8, PR-2/PR-3, SEC-A6). No LLM involved.

- list_pull_requests / get_pull_request: the repository's PRs and one PR's details (latest iteration, commits).
- get_pull_request_diff: the PR's changed files with the ADDED lines (line numbers of the new file), read from the
  git blobs of the latest iteration compared with the common base. Large, binary, lock and deleted files are skipped
  (and reported) so a big PR can never stall a chat turn.
- list_threads / create_thread: the PR's comment threads; review comments are posted as inline threads.
"""
import base64
import difflib
import json
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

MAX_FILES = 100                  # files read per review; the rest is listed as skipped
MAX_FILE_BYTES = 300_000
CONTEXT_LINES = 3                # unchanged lines kept around added lines (for the AI review)

LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "composer.lock",
              "gemfile.lock", "go.sum", "cargo.lock", "mix.lock", "pubspec.lock", "packages.lock.json", "uv.lock"}
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".pdf", ".zip", ".gz", ".tgz", ".jar",
                     ".war", ".dll", ".exe", ".so", ".dylib", ".class", ".woff", ".woff2", ".ttf", ".otf", ".eot",
                     ".mp3", ".mp4", ".mov", ".svgz", ".bin", ".keystore"}

# Tests replace these (httpx.MockTransport / no-op sleep).
transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None
_sleep = time.sleep


class PullRequestError(Exception):
    def __init__(self, message: str, *, error_type: str = "PR_FAILED", status_code: Optional[int] = None,
                 details: str = ""):
        super().__init__(message)
        self.message, self.error_type, self.status_code, self.details = message, error_type, status_code, details


@dataclass
class ChangedFile:
    path: str
    change_type: str                                    # add / edit / rename / delete ...
    added: Dict[int, str] = field(default_factory=dict)  # new-file line number -> text
    lines: List[str] = field(default_factory=list)       # the whole new file (for context)
    skipped: Optional[str] = None                        # why the file was not read


@dataclass
class PullRequestDiff:
    pr: Dict[str, Any]
    files: List[ChangedFile]

    @property
    def scanned(self) -> List[ChangedFile]:
        return [f for f in self.files if not f.skipped]

    @property
    def skipped(self) -> List[ChangedFile]:
        return [f for f in self.files if f.skipped]


@dataclass
class _Api:
    client: httpx.Client
    base: str            # https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}
    pat: str
    calls: int = 0

    def request(self, method: str, path: str, action: str, body: Any = None) -> httpx.Response:
        url = f"{self.base}/{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            try:
                self.calls += 1
                response = self.client.request(method, url, content=None if body is None else json.dumps(body),
                                               headers={"Content-Type": "application/json"})
            except httpx.TransportError as exc:
                if last:
                    raise PullRequestError(f"Network error while trying to {action}.", error_type="NETWORK_ERROR",
                                           details=sanitize_error(str(exc), self.pat)) from exc
                _sleep(2 ** attempt)
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and not last:
                _sleep(min(float(response.headers.get("Retry-After") or 2 ** attempt), 10.0))
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def check(self, response: httpx.Response, action: str, ok: Tuple[int, ...] = (200,), want_json: bool = True):
        content_type = response.headers.get("content-type", "")
        if response.status_code in ok and (not want_json or "json" in content_type):
            return response.json() if want_json else response.content
        status, body = response.status_code, sanitize_error(response.text, self.pat)[:500]
        if status == 401 or 300 <= status < 400 or (status < 300 and want_json and "json" not in content_type):
            raise PullRequestError(f"Azure DevOps authentication failed while trying to {action}. The PAT is missing, "
                                   "invalid or expired.", error_type="AUTHENTICATION_FAILED", status_code=status)
        if status == 403:
            raise PullRequestError(f"Permission denied while trying to {action}. The PAT needs the 'Code (Read & "
                                   "write)' scope.", error_type="PERMISSION_DENIED", status_code=status, details=body)
        if status == 404:
            raise PullRequestError(f"Not found while trying to {action}.", error_type="NOT_FOUND",
                                   status_code=status, details=body)
        raise PullRequestError(f"Azure DevOps returned status {status} while trying to {action}.",
                               error_type="BAD_REQUEST" if status == 400 else "HTTP_ERROR", status_code=status,
                               details=body)

    def get_json(self, path: str, action: str) -> Dict[str, Any]:
        return self.check(self.request("GET", path, action), action)


def _session(project: str, repository_id: str):
    config = get_ado_config()
    if not config.get("org_url") or not config.get("pat"):
        raise PullRequestError("Azure DevOps configuration missing (AZURE_DEVOPS_ORG_NAME / AZURE_DEVOPS_PAT).",
                               error_type="CONFIG_MISSING")
    pat = config["pat"]
    headers = {"Authorization": "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii"),
               "Accept": "application/json"}
    client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers, transport=transport_factory())
    base = f"{config['org_url']}/{quote(str(project))}/_apis/git/repositories/{quote(str(repository_id))}"
    return client, _Api(client=client, base=base, pat=pat), config["org_url"]


def _summary(pr: Dict[str, Any], web_base: str) -> Dict[str, Any]:
    ref = lambda name: (name or "").replace("refs/heads/", "")  # noqa: E731
    return {
        "id": pr.get("pullRequestId"),
        "title": pr.get("title") or "",
        "description": (pr.get("description") or "")[:2000],
        "author": (pr.get("createdBy") or {}).get("displayName") or "",
        "source": ref(pr.get("sourceRefName")),
        "target": ref(pr.get("targetRefName")),
        "status": pr.get("status") or "",
        "is_draft": bool(pr.get("isDraft")),
        "created": pr.get("creationDate") or "",
        "source_commit": (pr.get("lastMergeSourceCommit") or {}).get("commitId"),
        "target_commit": (pr.get("lastMergeTargetCommit") or {}).get("commitId"),
        "url": f"{web_base}/pullrequest/{pr.get('pullRequestId')}",
    }


def _web_base(org_url: str, pr: Dict[str, Any]) -> str:
    repo = pr.get("repository") or {}
    project = (repo.get("project") or {}).get("name") or ""
    return f"{org_url}/{quote(project)}/_git/{quote(repo.get('name') or '')}"


def list_pull_requests(project: str, repository_id: str, status: str = "active") -> List[Dict[str, Any]]:
    client, api, org_url = _session(project, repository_id)
    with client:
        data = api.get_json(f"pullrequests?searchCriteria.status={status}&$top=50", "list the pull requests")
    return [_summary(pr, _web_base(org_url, pr)) for pr in data.get("value") or []]


def get_pull_request(project: str, repository_id: str, pr_id: int) -> Dict[str, Any]:
    client, api, org_url = _session(project, repository_id)
    with client:
        return _get_pull_request(api, org_url, pr_id)


def _get_pull_request(api: _Api, org_url: str, pr_id: int) -> Dict[str, Any]:
    action = f"read pull request #{pr_id}"
    try:
        pr = api.get_json(f"pullrequests/{int(pr_id)}", action)
    except PullRequestError as exc:
        if exc.error_type == "NOT_FOUND":
            raise PullRequestError(f"Pull request #{pr_id} was not found in this repository.",
                                   error_type="PR_NOT_FOUND", status_code=404) from exc
        raise
    iterations = api.get_json(f"pullrequests/{int(pr_id)}/iterations", f"read the iterations of PR #{pr_id}")
    summary = _summary(pr, _web_base(org_url, pr))
    ids = [i.get("id") for i in iterations.get("value") or [] if i.get("id")]
    summary["iteration"] = max(ids) if ids else 1
    return summary


def _is_binary_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in BINARY_EXTENSIONS) or lower.endswith(".min.js") \
        or lower.endswith(".min.css") or lower.endswith(".map")


def skip_reason(path: str, change_type: str) -> Optional[str]:
    if "delete" in change_type:
        return "deleted"
    name = path.rsplit("/", 1)[-1].lower()
    if name in LOCK_FILES:
        return "lock file"
    if _is_binary_path(path):
        return "binary or minified"
    return None


def _decode(data: bytes) -> Optional[str]:
    if b"\x00" in data[:8000]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _blob(api: _Api, object_id: Optional[str], path: str) -> Optional[bytes]:
    if not object_id:
        return None
    response = api.request("GET", f"blobs/{object_id}?$format=octetstream", f"read {path}")
    return api.check(response, f"read {path}", want_json=False)


def added_lines(old_text: str, new_text: str) -> Dict[int, str]:
    """New-file line number (1-based) -> text, for every line the change added or replaced."""
    old, new = old_text.splitlines(), new_text.splitlines()
    result: Dict[int, str] = {}
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            for j in range(j1, j2):
                result[j + 1] = new[j]
    return result


def get_pull_request_diff(project: str, repository_id: str, pr_id: int) -> PullRequestDiff:
    client, api, org_url = _session(project, repository_id)
    with client:
        pr = _get_pull_request(api, org_url, pr_id)
        changes = api.get_json(f"pullrequests/{int(pr_id)}/iterations/{pr['iteration']}/changes?$top=2000&$compareTo=0",
                               f"read the changes of PR #{pr_id}")
        files: List[ChangedFile] = []
        read = 0
        for entry in changes.get("changeEntries") or []:
            item = entry.get("item") or {}
            if item.get("gitObjectType") not in (None, "blob") or item.get("isFolder"):
                continue
            path, change_type = item.get("path") or "", str(entry.get("changeType") or "edit").lower()
            changed = ChangedFile(path=path, change_type=change_type)
            files.append(changed)
            changed.skipped = skip_reason(path, change_type)
            if changed.skipped:
                continue
            if read >= MAX_FILES:
                changed.skipped = f"over the {MAX_FILES}-file limit"
                continue
            read += 1
            new_data = _blob(api, item.get("objectId"), path)
            if new_data is None:
                changed.skipped = "content not available"
                continue
            if len(new_data) > MAX_FILE_BYTES:
                changed.skipped = f"larger than {MAX_FILE_BYTES // 1000} KB"
                continue
            new_text = _decode(new_data)
            if new_text is None:
                changed.skipped = "binary"
                continue
            old_id = item.get("originalObjectId") or entry.get("originalObjectId")
            old_text = ""
            if old_id and "add" not in change_type:
                old_text = _decode(_blob(api, old_id, path) or b"") or ""
            changed.lines = new_text.splitlines()
            changed.added = added_lines(old_text, new_text)
        logger.info(f"[ADO PR] PR #{pr_id} iteration {pr['iteration']}: {len(files)} files changed, "
                    f"{sum(1 for f in files if not f.skipped)} read ({api.calls} requests)")
        return PullRequestDiff(pr=pr, files=files)


def hunks(changed: ChangedFile, context: int = CONTEXT_LINES) -> List[Tuple[int, int]]:
    """(first, last) new-file line ranges covering the added lines plus `context` lines around them."""
    ranges: List[Tuple[int, int]] = []
    for line in sorted(changed.added):
        start, end = max(1, line - context), min(len(changed.lines), line + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def list_threads(project: str, repository_id: str, pr_id: int) -> List[Dict[str, Any]]:
    client, api, _ = _session(project, repository_id)
    with client:
        data = api.get_json(f"pullrequests/{int(pr_id)}/threads", f"read the comments of PR #{pr_id}")
    return data.get("value") or []


def post_threads(project: str, repository_id: str, pr_id: int, threads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Posts each {"key", "content", "file"?, "line"?, "line_length"?} as a new active thread (inline when file+line are
    given). Returns {"posted": {key: thread id}, "error"?: {...}}; on an error part-way the threads posted so far are
    returned, so a retry continues without duplicates (the caller skips keys already posted).
    """
    client, api, _ = _session(project, repository_id)
    posted: Dict[str, int] = {}
    with client:
        for thread in threads:
            body: Dict[str, Any] = {"comments": [{"parentCommentId": 0, "content": thread["content"],
                                                  "commentType": 1}], "status": "active"}
            if thread.get("file") and thread.get("line"):
                line, length = int(thread["line"]), max(1, int(thread.get("line_length") or 1))
                body["threadContext"] = {"filePath": thread["file"],
                                         "rightFileStart": {"line": line, "offset": 1},
                                         "rightFileEnd": {"line": line, "offset": length + 1}}
            elif thread.get("file"):                       # a finding about the whole file (e.g. a committed .env)
                body["threadContext"] = {"filePath": thread["file"]}
            action = f"post a comment on PR #{pr_id}"
            try:
                data = api.check(api.request("POST", f"pullrequests/{int(pr_id)}/threads", action, body), action,
                                 ok=(200, 201))
            except PullRequestError as exc:
                logger.error(f"[ADO PR] Posting stopped after {len(posted)} threads: {exc.message} {exc.details}")
                return {"posted": posted, "error": {"message": exc.message, "error_type": exc.error_type}}
            posted[thread["key"]] = data.get("id")
    logger.info(f"[ADO PR] PR #{pr_id}: {len(posted)} comment threads posted")
    return {"posted": posted}
