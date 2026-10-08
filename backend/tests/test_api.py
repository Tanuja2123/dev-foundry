from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_cors_allows_frontend_origin() -> None:
    response = client.options(
        "/api/conversation",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_get_conversations() -> None:
    response = client.get("/api/conversation")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert "conversation_id" in payload[0]
    assert "title" in payload[0]


def test_create_and_delete_conversation() -> None:
    # Test POST /api/conversation
    create_res = client.post("/api/conversation")
    assert create_res.status_code == 200
    created = create_res.json()
    conv_id = created["conversation_id"]
    assert conv_id

    # Test GET /api/conversation/{id}
    get_res = client.get(f"/api/conversation/{conv_id}")
    assert get_res.status_code == 200
    assert get_res.json()["conv_id"] == conv_id

    # Test DELETE /api/conversation/{id}
    del_res = client.delete(f"/api/conversation/{conv_id}")
    assert del_res.status_code == 200

    # Verify deleted
    get_after_del = client.get(f"/api/conversation/{conv_id}")
    assert get_after_del.status_code == 404


def test_chat_endpoint_with_invalid_conversation() -> None:
    payload = {
        "conversation_id": "missing_conv_9999",
        "user_query": "This conversation does not exist.",
    }

    response = client.post("/api/chat", data=payload)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
