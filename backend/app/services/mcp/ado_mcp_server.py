import os
import base64
import re
import urllib.parse
from typing import Dict, Any, Optional
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

if __name__ == "__main__":
    mcp.run()
