import pytest
from app.utils.document_markdown import prd_json_to_markdown, trd_json_to_markdown
from app.schemas.project import ProjectOverview, DocumentDetails
from fastapi.testclient import TestClient
from app.main import app


def test_prd_json_to_markdown():
    sample_prd = {
        "Objective": "Build an employee attendance system",
        "Goals": ["Goal 1", "Goal 2"],
        "Functional Requirements": [
            {"id": "FR-001", "source": "User", "requirement": "Allow check-in and check-out"}
        ],
    }
    md = prd_json_to_markdown(sample_prd)
    assert "## Objective" in md
    assert "Build an employee attendance system" in md
    assert "## Goals" in md
    assert "- Goal 1" in md
    assert "FR-001" in md
    assert "Allow check-in and check-out" in md


def test_trd_json_to_markdown():
    sample_trd = {
        "Backend Technology Stack": {
            "Language": "Python",
            "Framework": "FastAPI",
            "Supporting Technologies": ["Pydantic", "Uvicorn"],
        }
    }
    md = trd_json_to_markdown(sample_trd)
    assert "## Backend Technology Stack" in md
    assert "**Language:** Python" in md
    assert "**Framework:** FastAPI" in md
    assert "- Pydantic" in md


def test_empty_markdown_converters():
    assert prd_json_to_markdown(None) == ""
    assert prd_json_to_markdown({}) == ""
    assert trd_json_to_markdown(None) == ""
    assert trd_json_to_markdown({}) == ""


def test_get_projects_api():
    client = TestClient(app)
    response = client.get("/api/projects")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    if data:
        item = data[0]
        assert "conversation_id" in item
        assert "created_at" in item
        assert "prd" in item
        assert "trd" in item
        assert item["prd"]["status"] in ["created", "not_created"]
        assert item["trd"]["status"] in ["created", "not_created"]
