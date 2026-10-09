import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: talks to real Azure DevOps (opt-in, set ADO_INTEGRATION_TESTS=1)")
    config.addinivalue_line("markers", "e2e: runs the real agent against Azure OpenAI + Azure DevOps (opt-in, set ADO_E2E_TESTS=1)")


import httpx
import pytest


@pytest.fixture
def no_real_network(monkeypatch):
    """Fails any real HTTP request (Azure DevOps, Supabase, Azure OpenAI). httpx.MockTransport is unaffected."""
    def blocked(self, request):
        raise RuntimeError(f"Real network access is blocked in unit tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def no_ado_config(monkeypatch):
    """Simulates missing Azure DevOps configuration (get_ado_config re-reads .env, so env vars alone are not enough)."""
    from app.services.mcp import ado_mcp_server
    monkeypatch.setattr(ado_mcp_server, "get_ado_config", lambda: {"org_name": None, "org_url": None, "pat": None})
