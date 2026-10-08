import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

import httpx

from app.core.logger import logger

# Azure DevOps Git "Pushes - Create" REST API
# https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pushes/create?view=azure-devops-rest-7.1
API_VERSION = "7.1"
ZERO_OBJECT_ID = "0" * 40

DEFAULT_COMMIT_MESSAGE = "chore: initial project scaffold"
DEFAULT_BRANCH = "main"

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2                      # extra attempts for 429 / 5xx / network errors
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 10.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Sanity limits for agent-supplied file lists
MAX_FILES = 200
MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 2_000_000
MAX_PATH_LENGTH = 400
MAX_COMMIT_MESSAGE_LENGTH = 1000


def get_default_commit_message() -> str:
    """Read at call time so values loaded from .env after import are honored."""
    return (os.getenv("ADO_SCAFFOLD_COMMIT_MESSAGE") or "").strip() or DEFAULT_COMMIT_MESSAGE


def get_default_branch() -> str:
    return (os.getenv("ADO_SCAFFOLD_DEFAULT_BRANCH") or "").strip() or DEFAULT_BRANCH


class AdoPushError(Exception):
    """Raised when files cannot be pushed. `details` holds the sanitized server response body."""

    def __init__(self, message: str, *, error_type: str = "PUSH_FAILED",
                 status_code: Optional[int] = None, details: str = ""):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "error",
            "error_type": self.error_type,
            "status_code": self.status_code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class _Context:
    client: httpx.Client
    repo_url: str
    pat: Optional[str]
    sleep: Callable[[float], None]
    max_retries: int
    retries_used: int = field(default=0)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """Normalizes a repo file path to '/a/b/c.ext' and rejects unsafe or folder-only paths."""
    if path is None or not str(path).strip():
        raise AdoPushError("File path must not be empty.", error_type="VALIDATION_ERROR")

    raw = str(path).strip().replace("\\", "/")
    if raw.endswith("/"):
        raise AdoPushError(f"Path '{path}' points to a folder. Provide a file path instead.",
                           error_type="VALIDATION_ERROR")
    if any(ord(ch) < 32 for ch in raw):
        raise AdoPushError(f"Path '{path}' contains control characters.", error_type="VALIDATION_ERROR")

    segments = [segment for segment in raw.split("/") if segment != ""]
    if not segments:
        raise AdoPushError(f"Path '{path}' is not a valid file path.", error_type="VALIDATION_ERROR")
    for segment in segments:
        if segment in (".", ".."):
            raise AdoPushError(f"Path '{path}' must not contain '.' or '..' segments.",
                               error_type="VALIDATION_ERROR")
        if segment.lower() == ".git":
            raise AdoPushError(f"Path '{path}' must not write inside a '.git' folder.",
                               error_type="VALIDATION_ERROR")

    normalized = "/" + "/".join(segments)
    if len(normalized) > MAX_PATH_LENGTH:
        raise AdoPushError(f"Path '{normalized[:60]}...' exceeds {MAX_PATH_LENGTH} characters.",
                           error_type="VALIDATION_ERROR")
    return normalized


def normalize_branch(branch: str) -> str:
    """Returns a short branch name ('main') from 'main' or 'refs/heads/main'."""
    name = (branch or "").strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    if (not name or name.startswith("/") or name.endswith("/") or name.endswith(".lock")
            or ".." in name or any(ch in name for ch in " ~^:?*[\\") or any(ord(ch) < 32 for ch in name)):
        raise AdoPushError(f"Branch name '{branch}' is not a valid Git branch name.", error_type="VALIDATION_ERROR")
    return name


def prepare_files(files: Mapping[str, str], *, max_files: int = MAX_FILES,
                  max_file_bytes: int = MAX_FILE_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES) -> Dict[str, str]:
    """Validates and normalizes a path -> content map. Returns a new map keyed by normalized path."""
    if not files:
        raise AdoPushError("At least one file is required.", error_type="VALIDATION_ERROR")
    if len(files) > max_files:
        raise AdoPushError(f"Too many files ({len(files)}). The limit is {max_files}.", error_type="VALIDATION_ERROR")

    prepared: Dict[str, str] = {}
    seen_lower: Dict[str, str] = {}
    total_bytes = 0
    for path, content in files.items():
        normalized = normalize_path(path)
        if normalized in prepared or normalized.lower() in seen_lower:
            raise AdoPushError(f"Duplicate file path '{normalized}'.", error_type="VALIDATION_ERROR")
        if not isinstance(content, str) or not content.strip():
            raise AdoPushError(f"File '{normalized}' has empty content. Every file must contain text.",
                               error_type="VALIDATION_ERROR")
        size = len(content.encode("utf-8"))
        if size > max_file_bytes:
            raise AdoPushError(f"File '{normalized}' is {size} bytes. The limit is {max_file_bytes} bytes.",
                               error_type="VALIDATION_ERROR")
        total_bytes += size
        prepared[normalized] = content
        seen_lower[normalized.lower()] = normalized

    if total_bytes > max_total_bytes:
        raise AdoPushError(f"Total content is {total_bytes} bytes. The limit is {max_total_bytes} bytes.",
                           error_type="VALIDATION_ERROR")
    return prepared


def build_push_body(branch: str, old_object_id: str, commit_message: str, files: Mapping[str, str]) -> Dict[str, Any]:
    """Builds the Pushes - Create request body: one commit containing one 'add' change per file."""
    return {
        "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": old_object_id}],
        "commits": [{
            "comment": commit_message,
            "changes": [
                {
                    "changeType": "add",
                    "item": {"path": path},
                    "newContent": {"content": content, "contentType": "rawtext"},
                }
                for path, content in files.items()
            ],
        }],
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _redact(text: str, pat: Optional[str]) -> str:
    from app.services.mcp.ado_mcp_server import sanitize_error
    return sanitize_error(text or "", pat)[:2000]


def _error_from_response(response: httpx.Response, pat: Optional[str], action: str) -> AdoPushError:
    status = response.status_code
    body = _redact(response.text, pat)
    content_type = response.headers.get("content-type", "")

    # Azure DevOps answers a missing/invalid/expired PAT with a redirect to an HTML sign-in page (verified: 302),
    # older endpoints may answer 203 + HTML or 401.
    if status == 401 or (300 <= status < 400) or (status < 300 and "json" not in content_type):
        return AdoPushError(f"Azure DevOps authentication failed while trying to {action}. "
                            "The PAT is missing, invalid or expired.",
                            error_type="AUTHENTICATION_FAILED", status_code=status, details=body[:300])
    if status == 403:
        return AdoPushError(f"Permission denied while trying to {action}. The PAT needs the "
                            "'Code (Read & write)' scope and the user needs Contribute permission on the repository.",
                            error_type="PERMISSION_DENIED", status_code=status, details=body)
    if status == 404:
        return AdoPushError(f"Project or repository not found while trying to {action}.",
                            error_type="NOT_FOUND", status_code=status, details=body)
    if status == 409:
        return AdoPushError(f"Conflict while trying to {action}: the branch was updated by someone else.",
                            error_type="CONFLICT", status_code=status, details=body)
    if status == 400:
        return AdoPushError(f"Azure DevOps rejected the request to {action}.",
                            error_type="BAD_REQUEST", status_code=status, details=body)
    return AdoPushError(f"Azure DevOps returned status {status} while trying to {action}.",
                        error_type="HTTP_ERROR", status_code=status, details=body)


def _retry_delay(response: Optional[httpx.Response], attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
    return RETRY_BACKOFF_SECONDS * (2 ** attempt)


def _send(ctx: _Context, method: str, url: str, action: str, json_body: Optional[Dict[str, Any]] = None,
          retry: bool = True) -> httpx.Response:
    """Sends a request, retrying 429/5xx/network errors when `retry` is set. Returns the final response."""
    attempts = (ctx.max_retries if retry else 0) + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            response = ctx.client.request(method, url, json=json_body)
        except httpx.TransportError as exc:
            if last:
                raise AdoPushError(f"Network error while trying to {action}.", error_type="NETWORK_ERROR",
                                   details=_redact(str(exc), ctx.pat)) from exc
            ctx.retries_used += 1
            delay = _retry_delay(None, attempt)
            logger.warning(f"[ADO Push] Network error while trying to {action}; retrying in {delay}s")
            ctx.sleep(delay)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and not last:
            ctx.retries_used += 1
            delay = _retry_delay(response, attempt)
            logger.warning(f"[ADO Push] Status {response.status_code} while trying to {action}; retrying in {delay}s")
            ctx.sleep(delay)
            continue
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def _json_or_error(ctx: _Context, response: httpx.Response, action: str, ok: Tuple[int, ...] = (200,)) -> Dict[str, Any]:
    if response.status_code not in ok or "json" not in response.headers.get("content-type", ""):
        raise _error_from_response(response, ctx.pat, action)
    return response.json()


def _get_repository(ctx: _Context) -> Dict[str, Any]:
    response = _send(ctx, "GET", f"{ctx.repo_url}?api-version={API_VERSION}", "read the repository")
    return _json_or_error(ctx, response, "read the repository")


def _get_branch_head(ctx: _Context, branch: str) -> Optional[str]:
    """Returns the head commit id of `branch`, or None if the branch does not exist."""
    # The refs `filter` is a prefix match (heads/main also returns heads/mainx), so match the full name exactly.
    url = f"{ctx.repo_url}/refs?filter={quote('heads/' + branch, safe='/')}&api-version={API_VERSION}"
    response = _send(ctx, "GET", url, "read branch refs")
    data = _json_or_error(ctx, response, "read branch refs")
    full_name = f"refs/heads/{branch}"
    for ref in data.get("value", []):
        if ref.get("name") == full_name:
            return ref.get("objectId")
    return None


def _get_commit_comment(ctx: _Context, commit_id: str) -> Optional[str]:
    response = _send(ctx, "GET", f"{ctx.repo_url}/commits/{commit_id}?api-version={API_VERSION}", "read a commit")
    if response.status_code != 200:
        return None
    return (response.json().get("comment") or "").strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def push_files_to_repo(
    project: str,
    repository: str,
    files: Mapping[str, str],
    commit_message: Optional[str] = None,
    branch: Optional[str] = None,
    *,
    allow_existing_branch: bool = True,
    org_url: Optional[str] = None,
    pat: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """
    Pushes every file in `files` (path -> text content) to `repository` as ONE commit.

    - Branch: `branch` if given, otherwise the repository's default branch, otherwise DEFAULT_BRANCH.
    - A branch that does not exist yet (e.g. an empty repo) is created with the zero object id.
    - An existing branch is updated from its current head, unless `allow_existing_branch` is False,
      in which case an ALREADY_INITIALIZED error is raised before anything is pushed.
    - If the branch moves between reading the head and pushing (409), the head is re-read and the push retried once.

    Returns a structured result dict. Raises AdoPushError with the sanitized server response on failure.
    """
    if not project or not str(project).strip():
        raise AdoPushError("Project name or id is required.", error_type="VALIDATION_ERROR")
    if not repository or not str(repository).strip():
        raise AdoPushError("Repository name or id is required.", error_type="VALIDATION_ERROR")

    message = (commit_message or "").strip() or get_default_commit_message()
    if len(message) > MAX_COMMIT_MESSAGE_LENGTH:
        raise AdoPushError(f"Commit message exceeds {MAX_COMMIT_MESSAGE_LENGTH} characters.",
                           error_type="VALIDATION_ERROR")
    prepared = prepare_files(files)
    requested_branch = normalize_branch(branch) if branch and branch.strip() else None

    if org_url is None or pat is None:
        from app.services.mcp.ado_mcp_server import get_ado_config
        config = get_ado_config()
        org_url = org_url or config.get("org_url")
        pat = pat or config.get("pat")
    if not org_url or not pat:
        raise AdoPushError("Azure DevOps configuration missing. AZURE_DEVOPS_ORG_NAME and AZURE_DEVOPS_PAT "
                           "environment variables must be configured.", error_type="CONFIGURATION_ERROR")

    org_url = org_url.rstrip("/")
    project_segment = quote(str(project).strip(), safe="")
    repo_segment = quote(str(repository).strip(), safe="")
    repo_url = f"{org_url}/{project_segment}/_apis/git/repositories/{repo_segment}"

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
    # Authorization is set per client; never logged.
    client.auth = httpx.BasicAuth("", pat)
    ctx = _Context(client=client, repo_url=repo_url, pat=pat, sleep=sleep, max_retries=max_retries)

    try:
        repo_info = _get_repository(ctx)
        default_ref = repo_info.get("defaultBranch")  # absent for an empty repository (verified)
        if requested_branch:
            target_branch = requested_branch
        elif default_ref:
            target_branch = normalize_branch(default_ref)
        else:
            target_branch = normalize_branch(get_default_branch())

        logger.info(f"[ADO Push] Pushing {len(prepared)} files to '{repository}' "
                    f"(project='{project}', branch='{target_branch}')")

        head = _get_branch_head(ctx, target_branch)
        conflict_retry_used = False
        while True:
            if head is not None and not allow_existing_branch:
                raise AdoPushError(
                    f"Branch '{target_branch}' already has commits in repository '{repository}'. "
                    "The starter structure was not pushed again, so nothing was changed.",
                    error_type="ALREADY_INITIALIZED", status_code=409)

            old_object_id = head or ZERO_OBJECT_ID
            body = build_push_body(target_branch, old_object_id, message, prepared)
            retries_before = ctx.retries_used
            response = _send(ctx, "POST", f"{repo_url}/pushes?api-version={API_VERSION}", "push the files", body)

            if response.status_code == 201:
                data = _json_or_error(ctx, response, "push the files", ok=(201,))
                commits = data.get("commits") or [{}]
                commit_id = commits[-1].get("commitId") or ""
                return _success(org_url, project, repository, target_branch, message, prepared, commit_id,
                                data.get("pushId"), data.get("url"), old_object_id == ZERO_OBJECT_ID)

            if response.status_code == 409:
                fresh_head = _get_branch_head(ctx, target_branch)
                # A transient failure may have hidden a push that actually landed: detect our own commit.
                if ctx.retries_used > retries_before and fresh_head and fresh_head != head:
                    if _get_commit_comment(ctx, fresh_head) == message:
                        logger.info("[ADO Push] Earlier attempt already applied the push; treating as success.")
                        return _success(org_url, project, repository, target_branch, message, prepared, fresh_head,
                                        None, None, head is None)
                if not conflict_retry_used:
                    conflict_retry_used = True
                    head = fresh_head
                    logger.warning(f"[ADO Push] Branch '{target_branch}' moved; re-read head and retrying once.")
                    continue

            raise _error_from_response(response, pat, "push the files")
    finally:
        if owns_client:
            client.close()


def _success(org_url: str, project: str, repository: str, branch: str, message: str, files: Mapping[str, str],
             commit_id: str, push_id: Any, push_url: Optional[str], created_branch: bool) -> Dict[str, Any]:
    project_segment = quote(str(project).strip(), safe="")
    repo_segment = quote(str(repository).strip(), safe="")
    repo_web_url = f"{org_url}/{project_segment}/_git/{repo_segment}"
    logger.info(f"[ADO Push] Pushed {len(files)} files to '{repository}' on '{branch}' (commit {commit_id[:8]})")
    return {
        "status": "success",
        "project": project,
        "repository": repository,
        "branch": branch,
        "created_branch": created_branch,
        "commit_id": commit_id,
        "commit_message": message,
        "files_count": len(files),
        "files": sorted(files.keys()),
        "push_id": push_id,
        "push_url": push_url,
        "commit_url": f"{repo_web_url}/commit/{commit_id}" if commit_id else None,
        "repository_url": repo_web_url,
    }
