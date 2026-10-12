"""
API tests against the real Supabase project (conversations are created and deleted again).
Authentication is replaced by a fixed test user; token validation itself is covered in test_auth_and_ownership.py.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.auth import CurrentUser, get_current_user
from app.main import app
from app.services.user_store import record_user

TEST_USER = CurrentUser(user_id="pytest-api-user", tenant_id="pytest", username="pytest@devfoundry.local",
                        display_name="API test user", email="pytest@devfoundry.local")

client = TestClient(app)


@pytest.fixture(autouse=True)
def signed_in_test_user():
    def current_user() -> CurrentUser:
        record_user(TEST_USER)  # the AppUser row must exist (Conversation.user_id references it)
        return TEST_USER

    app.dependency_overrides[get_current_user] = current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


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
    for item in payload:
        assert "conversation_id" in item
        assert "title" in item


def test_create_and_delete_conversation() -> None:
    # Test POST /api/conversation
    create_res = client.post("/api/conversation")
    assert create_res.status_code == 200
    created = create_res.json()
    conv_id = created["conversation_id"]
    assert conv_id

    # The new conversation is listed for its owner
    assert conv_id in [c["conversation_id"] for c in client.get("/api/conversation").json()]

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


def test_requests_without_sign_in_are_rejected() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    assert client.get("/api/conversation").status_code == 401
