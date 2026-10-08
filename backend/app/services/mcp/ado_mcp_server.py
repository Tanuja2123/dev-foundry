import os
import base64
import re
import urllib.parse
from typing import Dict, Any, Optional, List
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
                data = response.json()
                project_id = data.get("id") or data.get("operationId", "")
                project_url = f"{org_url}/{project_name}"
                logger.info(f"[ADO MCP] Azure DevOps project '{project_name}' created successfully.")
                return {
                    "status": "success",
                    "name": project_name,
                    "description": project_description,
                    "id": project_id,
                    "url": project_url
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

@mcp.tool(name="push_repository_scaffold", description="Push initial directory structure, starter boilerplate files, and .gitkeep placeholder files to an Azure DevOps Git repository.")
def push_repository_scaffold(
    project_id_or_name: str,
    repository_name_or_id: str,
    files: List[Dict[str, str]],
    commit_message: str = "Initial commit: Project structure scaffolding"
) -> Dict[str, Any]:
    """
    TOOL 3 — ADO REPOSITORY SCAFFOLD INITIALIZATION
    Pushes the approved file and folder structure directly to the Azure DevOps Git repository
    via the Azure DevOps Git Pushes REST API.
    """
    config = get_ado_config()
    org_url = config.get("org_url")
    pat = config.get("pat")

    logger.info(f"[ADO MCP] Tool invoked: push_repository_scaffold(project='{project_id_or_name}', repo='{repository_name_or_id}', file_count={len(files) if files else 0})")

    if not org_url or not pat:
        logger.warning("[ADO MCP] Azure DevOps organization name or PAT is missing in environment variables.")
        return {
            "status": "error",
            "message": "Azure DevOps configuration missing.",
            "details": "AZURE_DEVOPS_ORG_NAME and AZURE_DEVOPS_PAT environment variables must be configured."
        }

    # Normalize file paths and assemble Git changes array
    changes = []
    seen_paths = set()

    for f in (files or []):
        if not isinstance(f, dict):
            continue
        raw_path = str(f.get("path", "")).strip().replace("\\", "/")
        if not raw_path:
            continue

        if not raw_path.startswith("/"):
            raw_path = "/" + raw_path

        # If it points to an empty folder or ends with a slash, place a .gitkeep
        if raw_path.endswith("/"):
            raw_path = raw_path + ".gitkeep"

        if raw_path in seen_paths:
            continue
        seen_paths.add(raw_path)

        content = f.get("content")
        if content is None:
            content = ""

        changes.append({
            "changeType": "add",
            "item": {
                "path": raw_path
            },
            "newContent": {
                "content": str(content),
                "contentType": "rawtext"
            }
        })

    # Ensure at least README.md is present if no valid files were provided
    if not changes:
        changes.append({
            "changeType": "add",
            "item": {"path": "/README.md"},
            "newContent": {
                "content": f"# {repository_name_or_id}\n\nProject initialized with DevFoundry.",
                "contentType": "rawtext"
            }
        })

    auth_header = "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    encoded_project = urllib.parse.quote(str(project_id_or_name).strip())
    encoded_repo = urllib.parse.quote(str(repository_name_or_id).strip())

    try:
        with httpx.Client(timeout=45.0) as client:
            # Query refs to determine if refs/heads/main already has an initial objectId
            ref_url = f"{org_url}/{encoded_project}/_apis/git/repositories/{encoded_repo}/refs?filter=heads/main&api-version=7.1-preview.1"
            old_object_id = "0000000000000000000000000000000000000000"
            ref_resp = client.get(ref_url, headers=headers)
            if ref_resp.status_code == 200:
                refs_data = ref_resp.json().get("value", [])
                if refs_data and len(refs_data) > 0:
                    old_object_id = refs_data[0].get("objectId", old_object_id)

            push_url = f"{org_url}/{encoded_project}/_apis/git/repositories/{encoded_repo}/pushes?api-version=7.1-preview.2"
            payload = {
                "refUpdates": [
                    {
                        "name": "refs/heads/main",
                        "oldObjectId": old_object_id
                    }
                ],
                "commits": [
                    {
                        "comment": commit_message or "Initial commit: Project structure scaffolding",
                        "changes": changes
                    }
                ]
            }

            response = client.post(push_url, json=payload, headers=headers)
            if response.status_code in (200, 201):
                push_data = response.json()
                commit_id = ""
                commits = push_data.get("commits") or []
                if commits and isinstance(commits, list) and len(commits) > 0:
                    commit_id = commits[0].get("commitId", "")

                repo_url = f"{org_url}/{project_id_or_name}/_git/{repository_name_or_id}"
                logger.info(f"[ADO MCP] Pushed {len(changes)} files to repository '{repository_name_or_id}' (Commit: {commit_id}).")
                return {
                    "status": "success",
                    "repository": repository_name_or_id,
                    "branch": "main",
                    "commit_id": commit_id,
                    "pushed_files_count": len(changes),
                    "files": [c["item"]["path"] for c in changes],
                    "url": repo_url
                }
            else:
                raw_err = sanitize_error(response.text, pat)
                logger.error(f"[ADO MCP] Failed to push scaffold to repository '{repository_name_or_id}'. Status {response.status_code}: {raw_err}")
                return {
                    "status": "error",
                    "message": f"Azure DevOps repository scaffold push failed with status code {response.status_code}.",
                    "details": raw_err
                }
    except Exception as e:
        err_msg = sanitize_error(str(e), pat)
        logger.error(f"[ADO MCP] Exception during repository scaffold push: {err_msg}")
        return {
            "status": "error",
            "message": "Azure DevOps repository scaffold push encountered an unexpected error.",
            "details": err_msg
        }

if __name__ == "__main__":
    mcp.run()
