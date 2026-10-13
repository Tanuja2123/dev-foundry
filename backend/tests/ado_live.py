"""Helpers for opt-in tests that talk to a real Azure DevOps organization. Credentials come from the root .env."""
import os
import posixpath
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

API = "api-version=7.1"


def org_url() -> str:
    return "https://dev.azure.com/" + os.environ["AZURE_DEVOPS_ORG_NAME"].strip().strip("/")


def client() -> httpx.Client:
    return httpx.Client(auth=httpx.BasicAuth("", os.environ["AZURE_DEVOPS_PAT"]), timeout=60)


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def repo_url(project: str, repo: str) -> str:
    return f"{org_url()}/{quote(project, safe='')}/_apis/git/repositories/{quote(repo, safe='')}"


def get_repo(project: str, repo: str) -> dict:
    with client() as c:
        r = c.get(f"{repo_url(project, repo)}?{API}")
        r.raise_for_status()
        return r.json()


def list_tree(project: str, repo: str, branch: str) -> List[dict]:
    with client() as c:
        r = c.get(f"{repo_url(project, repo)}/items", params={
            "scopePath": "/", "recursionLevel": "Full", "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch", "api-version": "7.1"})
        r.raise_for_status()
        return r.json()["value"]


def list_commits(project: str, repo: str, branch: str) -> List[dict]:
    with client() as c:
        r = c.get(f"{repo_url(project, repo)}/commits", params={
            "searchCriteria.itemVersion.version": branch, "api-version": "7.1"})
        r.raise_for_status()
        return r.json()["value"]


def delete_repo(project: str, repo: str) -> None:
    with client() as c:
        info = c.get(f"{repo_url(project, repo)}?{API}")
        if info.status_code == 200:
            c.delete(f"{org_url()}/{quote(project, safe='')}/_apis/git/repositories/{info.json()['id']}?{API}")


def wait_for_project(project: str, timeout: float = 120) -> Optional[str]:
    """Waits until an asynchronously created project is wellFormed. Returns its id."""
    deadline = time.time() + timeout
    with client() as c:
        while time.time() < deadline:
            r = c.get(f"{org_url()}/_apis/projects/{quote(project, safe='')}?{API}")
            if r.status_code == 200 and r.json().get("state") == "wellFormed":
                return r.json()["id"]
            time.sleep(3)
    return None


def delete_project(project_id: str) -> int:
    with client() as c:
        return c.delete(f"{org_url()}/_apis/projects/{project_id}?{API}").status_code


def folders_of(paths) -> Set[str]:
    folders = set()
    for path in paths:
        parent = posixpath.dirname(path)
        while parent not in ("", "/"):
            folders.add(parent)
            parent = posixpath.dirname(parent)
    return folders


def render_tree(paths) -> str:
    """Renders file paths as an indented tree, for test evidence."""
    paths = set(paths)
    folders = folders_of(paths)
    lines = []
    for path in sorted(paths | folders):
        depth = path.count("/") - 1
        name = posixpath.basename(path) + ("/" if path in folders else "")
        lines.append("  " * depth + name)
    return "\n".join(lines)


def tree_paths(items: List[dict]) -> Dict[str, bool]:
    """path -> isFolder, without the root entry."""
    return {i["path"]: bool(i.get("isFolder")) for i in items if i["path"] != "/"}


def get_file_content(project: str, repo: str, path: str, branch: str) -> str:
    with client() as c:
        r = c.get(f"{repo_url(project, repo)}/items", headers={"Accept": "application/json"}, params={
            "path": path, "includeContent": "true", "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch", "api-version": "7.1"})
        r.raise_for_status()
        return r.json()["content"]


def get_file_bytes(project: str, repo: str, path: str, branch: str) -> bytes:
    """Raw bytes of a file as stored in the repository (works for binary and empty files)."""
    with client() as c:
        r = c.get(f"{repo_url(project, repo)}/items", params={
            "path": path, "download": "true", "$format": "octetStream", "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch", "api-version": "7.1"})
        r.raise_for_status()
        return r.content


def supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def delete_project_rows(conv_id: str) -> None:
    """Deletes the test's Project row (Repository rows cascade)."""
    supabase().table("Project").delete().eq("conv_id", conv_id).execute()
