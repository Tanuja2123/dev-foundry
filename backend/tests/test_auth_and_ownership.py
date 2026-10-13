"""Entra ID token validation, AppUser recording and per-user conversation ownership (no network)."""
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.core import auth
from app.main import app
from app.services import conversation_service as cs
from app.services import project_store as store
from app.services import user_store
from tests.fake_supabase import FakeSupabase

pytestmark = pytest.mark.usefixtures("no_real_network")

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"
BOB = "bbbbbbbb-0000-0000-0000-000000000002"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=_KEY.public_key())


def make_token(key=_KEY, **overrides):
    now = int(time.time())
    claims = {
        "aud": CLIENT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "iat": now, "nbf": now,
        "exp": now + 3600, "oid": ALICE, "tid": TENANT, "name": "Alice Example",
        "preferred_username": "alice@contoso.com", "ver": "2.0",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def bearer(**overrides):
    return {"Authorization": f"Bearer {make_token(**overrides)}"}


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("AZURE_AD_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", CLIENT)
    monkeypatch.setattr(auth, "_jwks_client", lambda tenant: FakeJwks())
    monkeypatch.setattr(cs, "get_supabase_client", lambda: fake)
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    monkeypatch.setattr(user_store, "client_factory", lambda: fake)
    user_store._last_recorded.clear()
    app.dependency_overrides.clear()
    return fake


@pytest.fixture
def client(db):
    return TestClient(app)


def seed_conversation(db, owner, title="Conv"):
    conv_id = str(uuid.uuid4())
    db.table("Conversation").insert({"conv_id": conv_id, "user_id": owner, "title": title, "content": [],
                                     "created_at": time.time()}).execute()
    return conv_id


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def test_valid_token_identifies_user_by_oid():
    user = auth.validate_token(make_token(), TENANT, CLIENT, FakeJwks())
    assert (user.user_id, user.tenant_id, user.username, user.display_name, user.email) == (
        ALICE, TENANT, "alice@contoso.com", "Alice Example", "alice@contoso.com")


@pytest.mark.parametrize("overrides, error", [
    ({"exp": int(time.time()) - 3600}, jwt.ExpiredSignatureError),
    ({"aud": "some-other-app"}, jwt.InvalidAudienceError),
    ({"iss": "https://login.microsoftonline.com/another-tenant/v2.0"}, jwt.InvalidIssuerError),
    ({"oid": None}, auth.AuthError),
    ({"tid": "another-tenant"}, auth.AuthError),
])
def test_invalid_tokens_are_rejected(overrides, error):
    with pytest.raises(error):
        auth.validate_token(make_token(**overrides), TENANT, CLIENT, FakeJwks())


def test_token_signed_with_another_key_is_rejected():
    with pytest.raises(jwt.InvalidSignatureError):
        auth.validate_token(make_token(key=_OTHER_KEY), TENANT, CLIENT, FakeJwks())


def test_unsigned_token_is_rejected():
    unsigned = jwt.encode({"aud": CLIENT, "oid": ALICE}, key=None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        auth.validate_token(unsigned, TENANT, CLIENT, FakeJwks())


def test_auth_settings_fall_back_to_frontend_values(monkeypatch):
    monkeypatch.delenv("AZURE_AD_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_AD_CLIENT_ID", raising=False)
    monkeypatch.setenv("VITE_AZURE_TENANT_ID", " tenant-from-vite ")
    monkeypatch.setenv("VITE_AZURE_CLIENT_ID", "client-from-vite")
    assert auth.get_auth_settings() == ("tenant-from-vite", "client-from-vite")


# ---------------------------------------------------------------------------
# API: authentication required
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("get", "/api/conversation"), ("post", "/api/conversation"),
    ("get", f"/api/conversation/{uuid.uuid4()}"), ("delete", f"/api/conversation/{uuid.uuid4()}"),
    ("get", "/api/projects"),
])
def test_every_api_route_requires_sign_in(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert client.post("/api/chat", data={"user_query": "hi"}).status_code == 401


def test_invalid_and_expired_tokens_get_401(client):
    assert client.get("/api/conversation", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    expired = client.get("/api/conversation", headers=bearer(exp=int(time.time()) - 3600))
    assert expired.status_code == 401 and "expired" in expired.json()["detail"]


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_missing_auth_configuration_is_a_server_error(client, monkeypatch):
    for name in ("AZURE_AD_TENANT_ID", "AZURE_AD_CLIENT_ID", "VITE_AZURE_TENANT_ID", "VITE_AZURE_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert client.get("/api/conversation", headers=bearer()).status_code == 500


def test_signed_in_user_is_recorded_in_app_user(client, db):
    assert client.get("/api/conversation", headers=bearer()).status_code == 200
    [row] = db.tables["AppUser"]
    assert (row["user_id"], row["username"], row["display_name"], row["tenant_id"]) == (
        ALICE, "alice@contoso.com", "Alice Example", TENANT)


def test_user_recording_failure_does_not_block_requests(client, monkeypatch):
    monkeypatch.setattr(user_store, "client_factory", lambda: FakeSupabase(missing_tables={"AppUser"}))
    assert client.get("/api/conversation", headers=bearer()).status_code == 200


# ---------------------------------------------------------------------------
# API: per-user ownership
# ---------------------------------------------------------------------------

def test_list_returns_only_my_conversations(client, db):
    mine = seed_conversation(db, ALICE, "Mine")
    seed_conversation(db, BOB, "Bob's")
    response = client.get("/api/conversation", headers=bearer())
    assert [c["conversation_id"] for c in response.json()] == [mine]


def test_projects_overview_returns_only_my_projects(client, db):
    alice_conv = seed_conversation(db, ALICE, "Alice Project")
    bob_conv = seed_conversation(db, BOB, "Bob Project")

    db.table("Project").insert({
        "conv_id": alice_conv,
        "project_name": "Alice App",
        "prd": {"Objective": "Alice Requirements"},
        "trd": {"Backend": "Python"},
    }).execute()

    db.table("Project").insert({
        "conv_id": bob_conv,
        "project_name": "Bob App",
        "prd": {"Objective": "Bob Requirements"},
        "trd": {"Backend": "Node"},
    }).execute()

    # Alice only sees her project
    alice_res = client.get("/api/projects", headers=bearer(oid=ALICE))
    assert alice_res.status_code == 200
    alice_projects = alice_res.json()
    assert len(alice_projects) == 1
    assert alice_projects[0]["conversation_id"] == alice_conv
    assert alice_projects[0]["project_name"] == "Alice App"

    # Bob only sees his project
    bob_res = client.get("/api/projects", headers=bearer(oid=BOB))
    assert bob_res.status_code == 200
    bob_projects = bob_res.json()
    assert len(bob_projects) == 1
    assert bob_projects[0]["conversation_id"] == bob_conv
    assert bob_projects[0]["project_name"] == "Bob App"


def test_rename_project_unauthorized(client):
    assert client.patch("/api/projects/some-id", json={"project_name": "New Name"}).status_code == 401


def test_rename_project_empty_name_returns_400(client, db):
    conv_id = seed_conversation(db, ALICE, "Initial Title")
    res = client.patch(f"/api/projects/{conv_id}", json={"project_name": "   "}, headers=bearer(oid=ALICE))
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


def test_rename_project_forbidden_for_other_user(client, db):
    bob_conv = seed_conversation(db, BOB, "Bob Title")
    res = client.patch(f"/api/projects/{bob_conv}", json={"project_name": "Hacked Name"}, headers=bearer(oid=ALICE))
    assert res.status_code == 400


def test_rename_project_success_and_syncs_conversation_title(client, db):
    alice_conv = seed_conversation(db, ALICE, "Initial Title")
    db.table("Project").insert({
        "conv_id": alice_conv,
        "project_name": "Initial Project",
        "prd": {"Objective": "Build something"},
        "trd": {"Backend": "FastAPI"},
    }).execute()

    res = client.patch(
        f"/api/projects/{alice_conv}",
        json={"project_name": "Updated Project Name"},
        headers=bearer(oid=ALICE),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["project_name"] == "Updated Project Name"

    # Verify Project table was updated
    project_row = db.table("Project").select("*").eq("conv_id", alice_conv).execute().data[0]
    assert project_row["project_name"] == "Updated Project Name"

    # Verify Conversation title was updated to maintain strict consistency
    conv_row = db.table("Conversation").select("*").eq("conv_id", alice_conv).execute().data[0]
    assert conv_row["title"] == "Updated Project Name"


def test_rename_project_updates_azure_devops_if_linked(client, db, monkeypatch):
    from unittest.mock import MagicMock
    import app.services.mcp.ado_mcp_server as ado_server

    mock_ado = MagicMock(return_value={
        "status": "success",
        "project_id": "ado-12345",
        "project_name": "New Azure DevOps Project",
        "url": "https://dev.azure.com/myorg/New%20Azure%20DevOps%20Project",
    })
    monkeypatch.setattr(ado_server, "update_ado_project", mock_ado)

    alice_conv = seed_conversation(db, ALICE, "Old ADO App")
    db.table("Project").insert({
        "conv_id": alice_conv,
        "project_name": "Old ADO App",
        "ado_project_id": "ado-12345",
        "ado_project_url": "https://dev.azure.com/myorg/Old%20ADO%20App",
    }).execute()

    res = client.patch(
        f"/api/projects/{alice_conv}",
        json={"project_name": "New Azure DevOps Project"},
        headers=bearer(oid=ALICE),
    )
    assert res.status_code == 200
    mock_ado.assert_called_once_with(project_id="ado-12345", project_name="New Azure DevOps Project")

    data = res.json()
    assert data["project_name"] == "New Azure DevOps Project"
    assert data["project_link"] == "https://dev.azure.com/myorg/New%20Azure%20DevOps%20Project"


def test_other_users_conversation_is_not_found(client, db):
    bobs = seed_conversation(db, BOB)
    assert client.get(f"/api/conversation/{bobs}", headers=bearer()).status_code == 404
    assert client.delete(f"/api/conversation/{bobs}", headers=bearer()).status_code == 404
    assert any(r["conv_id"] == bobs for r in db.tables["Conversation"])          # not deleted
    # Bob himself can open it
    assert client.get(f"/api/conversation/{bobs}", headers=bearer(oid=BOB)).status_code == 200


def test_create_and_delete_my_conversation(client, db):
    created = client.post("/api/conversation", headers=bearer()).json()
    [row] = [r for r in db.tables["Conversation"] if r["conv_id"] == created["conversation_id"]]
    assert row["user_id"] == ALICE
    assert client.delete(f"/api/conversation/{created['conversation_id']}", headers=bearer()).status_code == 200
    assert client.get(f"/api/conversation/{created['conversation_id']}", headers=bearer()).status_code == 404


def test_chat_cannot_continue_someone_elses_conversation(client, db, monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("the agent pipeline must not run for a foreign conversation")

    monkeypatch.setattr(cs, "generate_chat_response", must_not_run)
    bobs = seed_conversation(db, BOB)
    response = client.post("/api/chat", data={"conversation_id": bobs, "user_query": "hi"}, headers=bearer())
    assert response.status_code == 404


def test_first_chat_message_creates_a_conversation_owned_by_me(client, db, monkeypatch):
    async def reply(user_query, history, conversation_id=None):
        return "Hello!"

    monkeypatch.setattr(cs, "generate_chat_response", reply)
    response = client.post("/api/chat", data={"user_query": "Build a clinic app"}, headers=bearer())
    assert response.status_code == 200
    conv_id = response.json()["conversation_id"]
    [row] = [r for r in db.tables["Conversation"] if r["conv_id"] == conv_id]
    assert row["user_id"] == ALICE and len(row["content"]) == 2


def test_first_chat_message_with_null_conversation_id(client, db, monkeypatch):
    async def reply(user_query, history, conversation_id=None):
        return "Hello!"

    monkeypatch.setattr(cs, "generate_chat_response", reply)
    response = client.post("/api/chat", data={"user_query": "Build a clinic app", "conversation_id": "null"}, headers=bearer())
    assert response.status_code == 200
    conv_id = response.json()["conversation_id"]
    assert conv_id is not None and conv_id != "null"
    [row] = [r for r in db.tables["Conversation"] if r["conv_id"] == conv_id]
    assert row["user_id"] == ALICE and len(row["content"]) == 2


# ---------------------------------------------------------------------------
# Supabase key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key, role", [
    ("sb_secret_abc", "service_role"),
    ("sb_publishable_abc", "anon"),
    (jwt.encode({"role": "service_role"}, "s", algorithm="HS256"), "service_role"),
    (jwt.encode({"role": "anon"}, "s", algorithm="HS256"), "anon"),
    ("garbage", "unknown"),
])
def test_supabase_key_role(key, role):
    assert cs.supabase_key_role(key) == role
