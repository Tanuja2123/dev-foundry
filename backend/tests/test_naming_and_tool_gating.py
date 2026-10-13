"""
Names come from what the user approved, never from what a model invents:
- the Repo Setup Agent only gets the tools of the current operation (none for proposals);
- create tools use the stored, approved project / repository names;
- placeholder values ("placeholder", "<project name>", "TBD"...) are never stored or shown;
- the conversation title: the model's guess on the first message, then always the project name.
Regression for 2026-10-13: during "Let's proceed with repository setup" a fallback model created an ADO project
called "placeholder", and the sidebar title became "placeholder".
"""
import pytest

from app.services import conversation_service as cs
from app.services import project_store as store
from app.services.agents import repo_setup_agent as rsa
from tests.test_scaffold_service import ado, call, db  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("no_real_network")

CONV = "conv-naming-test"
PROPOSAL = ('{"status": "COMPLETE", "workflow_stage": "PROJECT_REVIEW", "project_proposal": '
            '{"project_name": "Developer CLI Toolkit", "project_description": "Python CLI for developers."}}')


def tool_names(operation):
    return [t.name for t in rsa.build_repo_setup_tools(CONV, operation)]


# ---------------------------------------------------------------------------
# Tool gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operation, expected", [
    ("GENERATE_PROJECT_PROPOSAL", []),
    ("UPDATE_PROJECT_PROPOSAL", []),
    ("GENERATE_REPOSITORY_PROPOSAL", []),
    ("UPDATE_REPOSITORY_PROPOSAL", []),
    ("CREATE_PROJECT", ["create_ado_project"]),
    ("UPDATE_ADO_PROJECT", ["update_ado_project"]),
    ("CREATE_REPOSITORY", ["create_ado_repository", "scaffold_repository_structure", "update_repository_structure"]),
    ("UPDATE_REPOSITORY_STRUCTURE", ["save_repository_structure", "scaffold_repository_structure",
                                     "update_repository_structure"]),
    ("PUSH_REPOSITORY_STRUCTURE", ["push_repository_structure"]),
])
def test_each_operation_only_gets_its_tools(operation, expected):
    assert tool_names(operation) == expected


def test_proposal_agent_has_no_tools_at_all():
    from agent_framework.openai import OpenAIChatCompletionClient
    client = OpenAIChatCompletionClient(model="t", api_key="k", azure_endpoint="https://x.openai.azure.com",
                                        api_version="2024-12-01-preview")
    agent = rsa.get_repo_setup_agent(client, CONV, "GENERATE_PROJECT_PROPOSAL")
    assert not agent.default_options.get("tools")
    assert len(rsa.get_repo_setup_agent(client, CONV).default_options["tools"]) == 7     # ungated (tests / tools)


# ---------------------------------------------------------------------------
# Create tools use the approved names
# ---------------------------------------------------------------------------

def test_create_project_uses_the_approved_proposal_not_the_models_arguments(db, ado):
    cs.persist_repo_setup_output(CONV, PROPOSAL)
    tools = rsa.build_repo_setup_tools(CONV, "CREATE_PROJECT")
    result = call(tools, "create_ado_project", project_name="placeholder", project_description="placeholder")
    assert result["status"] == "success"
    assert ado[0] == ("create_ado_project", "Developer CLI Toolkit")
    assert store.get_project(CONV)["project_name"] == "Developer CLI Toolkit"


def test_create_project_without_an_approved_proposal_is_refused(db, ado):
    tools = rsa.build_repo_setup_tools(CONV, "CREATE_PROJECT")
    result = call(tools, "create_ado_project", project_name="Anything", project_description="Anything at all")
    assert result["error_type"] == "NO_APPROVED_PROPOSAL" and ado == []


def test_placeholder_project_names_are_rejected_even_without_gating(db, ado):
    tools = rsa.build_repo_setup_tools(CONV)
    result = call(tools, "create_ado_project", project_name="<project name>", project_description="TBD")
    assert result["error_type"] == "VALIDATION_ERROR" and ado == []


def test_create_repository_uses_the_approved_repository_name(db, ado):
    cs.persist_repo_setup_output(CONV, PROPOSAL)
    call(rsa.build_repo_setup_tools(CONV, "CREATE_PROJECT"), "create_ado_project",
         project_name="x", project_description="x")
    cs.persist_repo_setup_output(CONV, '{"status": "COMPLETE", "workflow_stage": "REPOSITORY_REVIEW", '
                                       '"repository_proposal": {"repo_name": "dev-cli-toolkit"}}')
    result = call(rsa.build_repo_setup_tools(CONV, "CREATE_REPOSITORY"), "create_ado_repository",
                  project_id_or_name="whatever", repository_name="placeholder")
    assert result["status"] == "success" and ado[-1][2] == "dev-cli-toolkit"


def test_update_ado_project_rejects_placeholder_names(db, ado):
    cs.persist_repo_setup_output(CONV, PROPOSAL)
    call(rsa.build_repo_setup_tools(CONV, "CREATE_PROJECT"), "create_ado_project", project_name="x",
         project_description="x")
    result = call(rsa.build_repo_setup_tools(CONV, "UPDATE_ADO_PROJECT"), "update_ado_project",
                  project_name="placeholder")
    assert result["error_type"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Placeholders are never stored
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["placeholder", "Placeholder", "<project name>", "[Project Name]", "TBD", "N/A",
                                   "New Project", "untitled", "...", '"placeholder"', "", None])
def test_placeholder_values_are_detected(value):
    assert store.is_placeholder_name(value)


@pytest.mark.parametrize("value", ["Developer CLI Toolkit", "Placeholder Manager App", "TaskFlow", "Name Generator",
                                   "dev-cli-toolkit", "Test Automation Hub"])
def test_real_names_are_not_placeholders(value):
    assert not store.is_placeholder_name(value)


def test_placeholder_proposals_are_not_stored(db):
    cs.persist_repo_setup_output(CONV, '{"status": "COMPLETE", "workflow_stage": "PROJECT_REVIEW", "project_proposal": '
                                       '{"project_name": "placeholder", "project_description": "placeholder"}}')
    assert (store.get_project(CONV) or {}).get("project_name") is None


# ---------------------------------------------------------------------------
# Conversation title
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current, llm_title, query, project_name, expected", [
    # first message: the model's guess of the project name
    ("New Project", "Developer CLI Toolkit", "I want to build a CLI", None, "Developer CLI Toolkit"),
    # first message without a usable guess: first words of the message
    ("New Project", "placeholder", "I want to build a CLI for developers", None, "I want to build a"),
    ("New Project", None, "Build a clinic booking app", None, "Build a clinic booking app"),
    # later messages before a project name: unchanged (even if a model returns a title)
    ("Developer CLI Toolkit", "Something Else", "add a feature", None, "Developer CLI Toolkit"),
    # project name proposed / created / renamed: always the project name
    ("Developer CLI Toolkit", None, "ok", "DevKit CLI", "DevKit CLI"),
    ("I want to build a", None, "ok", "Developer CLI Toolkit", "Developer CLI Toolkit"),
    # a placeholder project name never becomes the title, and a placeholder title gets replaced
    ("Developer CLI Toolkit", None, "ok", "placeholder", "Developer CLI Toolkit"),
    ("placeholder", "Library Lending", "I want a library app", None, "Library Lending"),
    ("placeholder", None, "I want to build a command-line tool", None, "I want to build a"),
])
def test_conversation_title_rules(current, llm_title, query, project_name, expected):
    assert cs.resolve_conversation_title(current, llm_title, query, project_name) == expected


# ---------------------------------------------------------------------------
# JSON guard for the Requirements Agent (unreadable output was shown but never saved)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, ok", [
    ('{"status": "COMPLETE", "structured_project_response": {}}', True),
    ('```json\n{"status": "CLARIFICATION_REQUIRED"}\n```', True),
    ('{"status": "COMPLETE", "x": "unterminated}', False),
    ('{"structured_project_response": {}}', False),
    ("Here is your PRD", False),
    ("", False),
])
def test_agent_json_error(text, ok):
    assert (cs.agent_json_error(text) is None) is ok
