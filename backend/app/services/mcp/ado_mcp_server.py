import os
import base64
import re
import time
import urllib.parse
from typing import Dict, Any, List, Optional
from urllib.parse import quote
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from app.core.logger import logger

# Load environment variables from repo root and local paths
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../../../.env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../../.env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv()

# Initialize FastMCP Server for Azure DevOps
mcp = FastMCP("azure-devops-mcp")

def get_ado_config() -> Dict[str, Optional[str]]:
    """
    Retrieves Azure DevOps configuration securely from environment variables.
    Handles org names formatted as plain text or full URLs, and strips enclosing quotes.
    Credentials are NEVER hardcoded or exposed in logs or return structures.
    """
    # Ensure fresh read from environment
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../../../.env"))
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../../.env"))
    load_dotenv()

    raw_org = os.getenv("AZURE_DEVOPS_ORG_NAME")
    raw_pat = os.getenv("AZURE_DEVOPS_PAT")

    org_name = raw_org.strip().strip('"').strip("'") if raw_org else None
    pat = raw_pat.strip().strip('"').strip("'") if raw_pat else None

    org_url = None
    if org_name:
        if org_name.startswith("http://") or org_name.startswith("https://"):
            org_url = org_name.rstrip("/")
            clean_org_name = org_url.split("/")[-1]
        else:
            clean_org_name = org_name.strip("/")
            org_url = f"https://dev.azure.com/{clean_org_name}"
    else:
        clean_org_name = None

    logger.info(f"[ADO Config] Organization: '{clean_org_name}', URL: '{org_url}', PAT configured: {bool(pat)}")

    return {
        "org_name": clean_org_name,
        "org_url": org_url,
        "pat": pat
    }



def sanitize_error(text: str, pat: Optional[str] = None) -> str:
    """Removes any potential credentials or PAT occurrences from error strings."""
    if not text:
        return ""
    sanitized = text
    if pat:
        sanitized = sanitized.replace(pat, "[REDACTED_PAT]")
    # Redact any basic auth headers or token patterns
    sanitized = re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", sanitized)
    return sanitized

PROJECT_CREATION_TIMEOUT_SECONDS = 90
PROJECT_POLL_INTERVAL_SECONDS = 2
_sleep = time.sleep

def wait_for_project_creation(client: httpx.Client, org_url: str, operation_id: Optional[str], project_name: str,
                              headers: Dict[str, str]) -> Dict[str, Any]:
    """
    Polls Operations - Get until the queued project creation finishes, then reads the project.
    Returns {"status": "success", "id": <project id>, "url": <web url>} or an error dict.
    https://learn.microsoft.com/en-us/rest/api/azure/devops/operations/operations/get?view=azure-devops-rest-7.1
    """
    if operation_id:
        deadline = time.monotonic() + PROJECT_CREATION_TIMEOUT_SECONDS
        while True:
            op = client.get(f"{org_url}/_apis/operations/{operation_id}?api-version=7.1", headers=headers)
            status = op.json().get("status") if op.status_code == 200 else None
            if status == "succeeded":
                break
            if status in ("failed", "cancelled"):
                body = op.json()
                return {
                    "status": "error",
                    "message": f"Azure DevOps project creation {status}.",
                    "details": body.get("detailedMessage") or body.get("resultMessage") or ""
                }
            if time.monotonic() >= deadline:
                return {
                    "status": "error",
                    "error_type": "PROVISIONING_TIMEOUT",
                    "message": f"Azure DevOps is still creating project '{project_name}'. Check again in a minute before retrying.",
                    "details": f"Operation {operation_id} status: {status}"
                }
            _sleep(PROJECT_POLL_INTERVAL_SECONDS)

    project = client.get(f"{org_url}/_apis/projects/{quote(project_name, safe='')}?api-version=7.1", headers=headers)
    if project.status_code != 200:
        return {
            "status": "error",
            "message": f"Project '{project_name}' was queued but could not be read back (status {project.status_code}).",
            "details": project.text[:500]
        }
    data = project.json()
    web_url = (data.get("_links") or {}).get("web", {}).get("href") or f"{org_url}/{quote(project_name)}"
    return {"status": "success", "id": data.get("id"), "url": web_url}

@mcp.tool(name="create_ado_project", description="Create a new Azure DevOps project using the approved project name and description.")
def create_ado_project(project_name: str, project_description: str) -> Dict[str, Any]:
    """
    TOOL 1 — ADO PROJECT CREATION
    Creates an Azure DevOps project using the approved project name and description.
    """
    config = get_ado_config()
    org_url = config.get("org_url")
    pat = config.get("pat")
    
    logger.info(f"[ADO MCP] Tool invoked: create_ado_project(project_name='{project_name}')")

    if not org_url or not pat:
        logger.warning("[ADO MCP] Azure DevOps organization name or PAT is missing in environment variables.")
        return {
            "status": "error",
            "message": "Azure DevOps configuration missing.",
            "details": "AZURE_DEVOPS_ORG_NAME and AZURE_DEVOPS_PAT environment variables must be configured."
        }

    auth_header = "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    url = f"{org_url}/_apis/projects?api-version=7.1-preview.4"
    payload = {
        "name": project_name,
        "description": project_description,
        "capabilities": {
            "versioncontrol": {"sourceControlType": "Git"},
            "processTemplate": {"templateTypeId": "6b724908-ef14-45cf-84f8-768b5384da45"}  # Agile default template
        }
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)
            
            if response.status_code in (200, 202):
                # The create call only queues the project and returns an OperationReference: its "id" is the
                # operation id, not the project id. Wait for the operation, then read the real project.
                operation_id = response.json().get("id")
                created = wait_for_project_creation(client, org_url, operation_id, project_name, headers)
                if created.get("status") == "error":
                    created["details"] = sanitize_error(created.get("details", ""), pat)
                    logger.error(f"[ADO MCP] Project '{project_name}' was not created: {created['message']}")
                    return created
                logger.info(f"[ADO MCP] Azure DevOps project '{project_name}' created successfully.")
                return {
                    "status": "success",
                    "name": project_name,
                    "description": project_description,
                    "id": created["id"],
                    "url": created["url"]
                }
            else:
                raw_err = sanitize_error(response.text, pat)
                logger.error(f"[ADO MCP] Failed to create project '{project_name}'. Status {response.status_code}: {raw_err}")
                return {
                    "status": "error",
                    "message": f"Azure DevOps project creation failed with status code {response.status_code}.",
                    "details": raw_err
                }
    except Exception as e:
        err_msg = sanitize_error(str(e), pat)
        logger.error(f"[ADO MCP] Exception during project creation: {err_msg}")
        return {
            "status": "error",
            "message": "Azure DevOps project creation encountered an unexpected error.",
            "details": err_msg
        }

@mcp.tool(name="create_ado_repository", description="Create a new Git repository within the specified Azure DevOps project using the approved repository name.")
def create_ado_repository(project_id_or_name: str, repository_name: str) -> Dict[str, Any]:
    """
    TOOL 2 — ADO REPOSITORY CREATION
    Creates the repository in the newly created Azure DevOps project using the approved repository configuration.
    """
    config = get_ado_config()
    org_url = config.get("org_url")
    pat = config.get("pat")
    
    logger.info(f"[ADO MCP] Tool invoked: create_ado_repository(project='{project_id_or_name}', repository_name='{repository_name}')")

    if not org_url or not pat:
        logger.warning("[ADO MCP] Azure DevOps organization name or PAT is missing in environment variables.")
        return {
            "status": "error",
            "message": "Azure DevOps configuration missing.",
            "details": "AZURE_DEVOPS_ORG_NAME and AZURE_DEVOPS_PAT environment variables must be configured."
        }

    auth_header = "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    encoded_project = urllib.parse.quote(str(project_id_or_name).strip())
    url = f"{org_url}/{encoded_project}/_apis/git/repositories?api-version=7.1-preview.1"
    payload = {
        "name": repository_name
    }


    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)
            
            if response.status_code in (200, 201):
                data = response.json()
                repo_id = data.get("id", "")
                repo_url = data.get("webUrl") or data.get("remoteUrl") or f"{org_url}/{project_id_or_name}/_git/{repository_name}"
                logger.info(f"[ADO MCP] Azure DevOps repository '{repository_name}' created successfully.")
                return {
                    "status": "success",
                    "name": repository_name,
                    "id": repo_id,
                    "url": repo_url
                }
            else:
                raw_err = sanitize_error(response.text, pat)
                logger.error(f"[ADO MCP] Failed to create repository '{repository_name}'. Status {response.status_code}: {raw_err}")
                return {
                    "status": "error",
                    "message": f"Azure DevOps repository creation failed with status code {response.status_code}.",
                    "details": raw_err
                }
    except Exception as e:
        err_msg = sanitize_error(str(e), pat)
        logger.error(f"[ADO MCP] Exception during repository creation: {err_msg}")
        return {
            "status": "error",
            "message": "Azure DevOps repository creation encountered an unexpected error.",
            "details": err_msg
        }

@mcp.tool(name="push_repository_files", description="Push a list of files (path + non-empty content) to a newly created, empty Azure DevOps repository as a single initial commit.")
def push_repository_files(project_id_or_name: str, repository_name: str, files: List[Dict[str, str]],
                          commit_message: Optional[str] = None, branch: Optional[str] = None) -> Dict[str, Any]:
    """
    TOOL 3 — REPOSITORY FILE PUSH
    Pushes caller-supplied files ([{"path": ..., "content": ...}]) as one commit (validated paths, non-empty content,
    size limits). Refuses (without changing anything) if the target branch already has commits.
    """
    from app.services.mcp.ado_git_push import AdoPushError, push_files_to_repo

    logger.info(f"[ADO MCP] Tool invoked: push_repository_files(project='{project_id_or_name}', "
                f"repository_name='{repository_name}', files={len(files or [])})")
    try:
        file_map: Dict[str, str] = {}
        for index, item in enumerate(files or []):
            data = item if isinstance(item, dict) else (item.model_dump() if hasattr(item, "model_dump") else {})
            path, content = data.get("path"), data.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise AdoPushError(f"File #{index + 1} must have string 'path' and 'content' fields.",
                                   error_type="VALIDATION_ERROR")
            if path in file_map:
                raise AdoPushError(f"Duplicate file path '{path}'.", error_type="VALIDATION_ERROR")
            file_map[path] = content
        return push_files_to_repo(project_id_or_name, repository_name, file_map, commit_message=commit_message,
                                  branch=branch, allow_existing_branch=False)
    except AdoPushError as e:
        logger.error(f"[ADO MCP] Custom file push failed: {e.error_type}: {e.message}")
        return e.to_dict()
    except Exception as e:
        err_msg = sanitize_error(str(e), get_ado_config().get("pat"))
        logger.error(f"[ADO MCP] Exception during custom file push: {err_msg}")
        return {"status": "error", "error_type": "UNEXPECTED_ERROR",
                "message": "Custom file push encountered an unexpected error.", "details": err_msg}


if __name__ == "__main__":
    mcp.run()
