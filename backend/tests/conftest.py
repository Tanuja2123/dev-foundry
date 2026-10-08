import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: talks to real Azure DevOps (opt-in, set ADO_INTEGRATION_TESTS=1)")
    config.addinivalue_line("markers", "e2e: runs the real agent against Azure OpenAI + Azure DevOps (opt-in, set ADO_E2E_TESTS=1)")
