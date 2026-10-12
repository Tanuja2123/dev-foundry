from typing import Literal, Optional
from pydantic import BaseModel, Field


class DocumentDetails(BaseModel):
    """Status and markdown content for a single document (PRD or TRD)."""

    status: Literal["created", "not_created"] = Field(
        ..., description="Whether the document has been generated"
    )
    content: Optional[str] = Field(
        None, description="Markdown-formatted document content"
    )
    updated_at: Optional[str] = Field(
        None, description="ISO 8601 timestamp of when the project row was last updated"
    )


class ProjectOverview(BaseModel):
    """One row in the Projects table view — mirrors frontend/src/types.ts ProjectOverview."""

    conversation_id: str = Field(..., description="conv_id from the Project table")
    project_name: Optional[str] = Field(None, description="Auto-generated project name")
    description: Optional[str] = Field(None, description="Short project description")
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    prd: DocumentDetails
    trd: DocumentDetails
    project_link: Optional[str] = Field(None, description="Azure DevOps project URL")
    repo_link: Optional[str] = Field(None, description="Azure DevOps Git repository URL")
