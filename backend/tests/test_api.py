from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_get_conversations() -> None:
    response = client.get("/api/conversation")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert {item["conversation_id"] for item in payload}.issuperset({"conv_001", "conv_002"})


def test_chat_endpoint() -> None:
    payload = {
        "conversation_id": "conv_001",
        "user_query": "Create a requirement document for the project automation agent.",
    }

    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == "conv_001"
    assert "Demo AI response" in data["response"]


def test_chat_endpoint_with_invalid_conversation() -> None:
    payload = {
        "conversation_id": "missing_conv",
        "user_query": "This conversation does not exist.",
    }

    response = client.post("/api/chat", json=payload)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
