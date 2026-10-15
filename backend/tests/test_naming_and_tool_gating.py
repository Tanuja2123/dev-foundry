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

@pytest.mark.parametrize("current, llm_title, first_message, official_name, expected", [
    # a confident product name from the model names the chat
    ("New Project", "Developer CLI Toolkit", "I want to build a CLI", None, "Developer CLI Toolkit"),
    # no (confident) product name: stays "New Project" - the user's words never become the title (seen live)
    ("New Project", None, "what are you up to nowadays?", None, "New Project"),
    ("New Project", "placeholder", "I want to build a CLI for developers", None, "New Project"),
    ("New Project", None, "hiii", None, "New Project"),
    # a title copied from the first message by older versions is temporary: replaced, or reset to the default
    ("what are you up to", None, "what are you up to nowadays?", None, "New Project"),
    ("what are you up to", "Clinic Appointment Booking", "what are you up to nowadays?", None,
     "Clinic Appointment Booking"),
    # later messages: a named chat keeps its name (even if the model suggests another one)
    ("Developer CLI Toolkit", "Something Else", "I want to build a CLI", None, "Developer CLI Toolkit"),
    # the finalized Azure DevOps project name always wins, and follows renames
    ("Developer CLI Toolkit", None, "I want to build a CLI", "DevKit CLI", "DevKit CLI"),
    ("DevKit CLI", None, "I want to build a CLI", "DevKit CLI v2", "DevKit CLI v2"),
    ("New Project", None, "hello", "Clinic-Booking", "Clinic-Booking"),
    # placeholders never become titles; placeholder titles are temporary
    ("Developer CLI Toolkit", None, "x", "placeholder", "Developer CLI Toolkit"),
    ("placeholder", "Library Lending", "I want a library app", None, "Library Lending"),
    ("hiii", "School Management System", "hiii", None, "School Management System"),
])
def test_conversation_title_rules(current, llm_title, first_message, official_name, expected):
    assert cs.resolve_conversation_title(current, llm_title, first_message, official_name) == expected


@pytest.mark.parametrize("title, confidence, expected", [
    ("Clinic Appointment Booking", 0.85, "Clinic Appointment Booking"),
    ("Clinic Appointment Booking", 0.7, "Clinic Appointment Booking"),
    ("Management System", 0.6, None),                 # below the threshold
    ("What Are You Up To", 0.0, None),                # small talk
    ("placeholder", 0.95, None),
    (None, 0.9, None),
    ("School Management System", 9, "School Management System"),   # a 0-10 scale is tolerated
])
def test_title_needs_enough_confidence(title, confidence, expected):
    from app.services.workflow_routing import RoutingDecision
    decision = RoutingDecision(reasoning="r", action="respond", reply="x", title=title, title_confidence=confidence)
    assert cs.inferred_title(decision) == expected


def test_title_threshold_is_configurable(monkeypatch):
    from app.services.workflow_routing import RoutingDecision
    decision = RoutingDecision(reasoning="r", action="respond", reply="x", title="Clinic App", title_confidence=0.75)
    monkeypatch.setenv("TITLE_CONFIDENCE_THRESHOLD", "0.9")
    assert cs.inferred_title(decision) is None
    monkeypatch.setenv("TITLE_CONFIDENCE_THRESHOLD", "0.5")
    assert cs.inferred_title(decision) == "Clinic App"


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


# ---------------------------------------------------------------------------
# Tech-change check: a change to a clearly weaker fit needs the user's confirmation
# ---------------------------------------------------------------------------

import json  # noqa: E402


def assessment_output(current, requested, confirmed=False):
    return json.dumps({"status": "COMPLETE", "workflow_stage": "TRD", "technical_requirements_document": {"x": 1},
                       "change_notes": "Backend language: Python -> Go",
                       "tech_change_assessment": {"requested_change": "Backend language: Python -> Go",
                                                  "current_fit": current, "requested_fit": requested,
                                                  "pros": ["Single binary"], "cons": ["No Typer/Rich ecosystem"],
                                                  "recommendation": "Keep Python.", "user_confirmed": confirmed}})


@pytest.mark.parametrize("current, requested, confirmed, needs", [
    (8, 4, False, True),     # clearly worse -> confirm first
    (8, 5, False, True),     # gap 3 = threshold
    (8, 6, False, False),    # close enough -> apply directly
    (6, 9, False, False),    # better -> apply directly
    (8, 3, True, False),     # user already confirmed after the warning -> apply
    ("8", "x", False, False),  # unusable scores -> never block
])
def test_tech_change_confirmation_threshold(current, requested, confirmed, needs):
    assert (cs.tech_change_needing_confirmation(assessment_output(current, requested, confirmed)) is not None) is needs


def test_tech_change_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("TECH_CHANGE_CONFIRM_THRESHOLD", "5")
    assert cs.tech_change_needing_confirmation(assessment_output(8, 4)) is None
    assert cs.tech_change_needing_confirmation(assessment_output(9, 4)) is not None


def test_no_assessment_means_no_confirmation():
    assert cs.tech_change_needing_confirmation(json.dumps({"status": "COMPLETE", "technical_requirements_document": {}})) is None
