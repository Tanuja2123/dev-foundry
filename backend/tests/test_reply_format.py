"""Reply format: every reply ends with a divider and ONE plain closing sentence (enforced in code, not left to the model)."""
import pytest

from app.services import conversation_service as cs

pytestmark = pytest.mark.usefixtures("no_real_network")


@pytest.mark.parametrize("raw, expected", [
    # divider after the closing sentence (seen live) -> moved before it
    ("## TRD\n\nContent.\n\nWould you like to change anything?\n\n---",
     "## TRD\n\nContent.\n\n---\n\nWould you like to change anything?"),
    # no divider at all -> added
    ("### Proposed Project Name\n\nShopFlow\n\nPlease review it.",
     "### Proposed Project Name\n\nShopFlow\n\n---\n\nPlease review it."),
    # already correct -> unchanged
    ("Body.\n\n---\n\nNext step.", "Body.\n\n---\n\nNext step."),
    # repeated dividers -> one
    ("Body.\n\n---\n\n---\n\nNext step.\n\n---\n\n---", "Body.\n\n---\n\nNext step."),
    # a closing question, the divider, then another closing sentence (seen live) -> only the question stays
    ("Body.\n\nWould you like to change anything?\n\n---\n\nYou can let me know if you'd like to adjust.",
     "Body.\n\n---\n\nWould you like to change anything?"),
    # ends with a list / code -> left alone (there is no closing sentence to separate)
    ("Options:\n\n1. **A** - x\n2. **B** - y", "Options:\n\n1. **A** - x\n2. **B** - y"),
    ("Tree:\n\n```text\na/\n```", "Tree:\n\n```text\na/\n```"),
    # a single sentence -> nothing to separate
    ("Just one sentence.", "Just one sentence."),
    # one paragraph with several sentences (seen live) -> the last sentence becomes the closing line
    ("I'm **DevFoundry**, an AI assistant for requirements, PRDs and Azure DevOps setup. Think of me as your "
     "automation partner. If you have a project in mind, just let me know!",
     "I'm **DevFoundry**, an AI assistant for requirements, PRDs and Azure DevOps setup. Think of me as your "
     "automation partner.\n\n---\n\nIf you have a project in mind, just let me know!"),
    # a multi-sentence last paragraph after other content -> only its last sentence is the closing line
    ("## TRD\n\nContent.\n\nThe stack fits the PRD. Would you like to change anything?",
     "## TRD\n\nContent.\n\nThe stack fits the PRD.\n\n---\n\nWould you like to change anything?"),
    # two closing lines around the divider, the first an invitation (seen live in the project proposal)
    ("### Proposed Project Name\n\nClinic\n\nPlease review the name and description and tell me what to change, or "
     "approve them to create the Azure DevOps project.\n\n---\n\nLet me know any adjustments or confirm to move forward.",
     "### Proposed Project Name\n\nClinic\n\n---\n\nPlease review the name and description and tell me what to change, "
     "or approve them to create the Azure DevOps project."),
    # the same without a divider
    ("Body.\n\nPlease review the draft.\n\nLet me know if anything should change.",
     "Body.\n\n---\n\nPlease review the draft."),
    # a content sentence before the closing line stays
    ("Body.\n\nNothing was changed.\n\nLet me know if anything should change.",
     "Body.\n\nNothing was changed.\n\n---\n\nLet me know if anything should change."),
    # abbreviations and version numbers are not sentence ends
    ("Use e.g. Python 3.12 for the CLI. Shall I continue?", "Use e.g. Python 3.12 for the CLI.\n\n---\n\nShall I continue?"),
])
def test_enforce_closing_line(raw, expected):
    assert cs.enforce_closing_line(raw) == expected


PRD = {"Objective": "Let patients book clinic appointments online.",
       "Functional Requirements": [{"id": "FR-001", "requirement": "The system shall let patients book"}]}
TRD = {"Backend Technology Stack": {"Framework": "FastAPI", "Rationale": "Fits the booking API (FR-001)."}}


def test_stored_prd_is_shown_from_the_database():
    reply = cs.stored_document_reply("PRD", {"project": {"prd": PRD, "prd_version": 2}})
    assert reply.startswith("Here is the current, suggested **Product Requirements Document (PRD)** (version 2):")
    assert "Let patients book clinic appointments online." in reply and "FR-001" in reply
    assert reply.rstrip().endswith("ask me to create the TRD when you're ready.")
    assert "\n\n---\n\n" in reply


def test_stored_trd_is_shown_and_missing_documents_fall_back_to_the_model_reply():
    reply = cs.stored_document_reply("TRD", {"project": {"prd": PRD, "trd": TRD, "trd_version": 1}})
    assert "**Technical Requirements Document (TRD)** (version 1)" in reply and "FastAPI" in reply
    assert cs.stored_document_reply("TRD", {"project": {"prd": PRD}}) is None      # no TRD yet
    assert cs.stored_document_reply("PRD", {"project": None}) is None
    assert cs.stored_document_reply(None, {"project": {"prd": PRD}}) is None


def test_escaped_line_breaks_in_replies_are_restored():
    assert cs.normalize_reply("# PRD\\n\\n**Project:** Clinic") == "# PRD\n\n**Project:** Clinic"
    assert cs.normalize_reply("Use C:\\new folder\nnext line\nthird") == "Use C:\\new folder\nnext line\nthird"
    assert cs.normalize_reply("Hello") == "Hello"


def test_orchestrator_prompt_covers_showing_documents_and_a_second_product():
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "respond with show_document" in source
    assert "One chat holds ONE project" in source
    from app.services.workflow_routing import RoutingDecision
    assert RoutingDecision(reasoning="r", action="respond", reply="x", show_document="PRD").show_document == "PRD"


def test_requirements_without_a_source_render_cleanly():
    reply = cs.stored_document_reply("PRD", {"project": {"prd": PRD}})
    assert "- **FR-001**: The system shall let patients book" in reply and "__" not in reply


def test_the_answer_to_the_different_product_question_is_not_asked_again():
    from app.services.workflow_routing import answers_different_product_question, different_product_reply
    question = different_product_reply({"project": {"prd": PRD}})
    assert question.startswith("This chat already has a PRD, and your message describes a different product.")
    assert "*" not in question.split("\n\n")[0]                     # no italic objective quote any more
    assert question.rstrip().endswith("Shall I continue with the current project?")
    history = [{"role": "user", "text": "Build a school management system."}, {"role": "assistant", "text": question}]
    assert answers_different_product_question(history)
    assert not answers_different_product_question(history + [{"role": "user", "text": "yes, create the TRD"},
                                                             {"role": "assistant", "text": "Here is the TRD"}])
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "answers_different_product_question(history)" in source
    assert '"yes, create\n     the TRD" -> GENERATE_TRD' in source


def test_a_different_product_never_overwrites_the_existing_prd():
    from app.services.workflow_routing import RoutingDecision, apply_intent_rules
    state = {"project": {"prd": PRD, "project_name": None}}
    for decision in (
            RoutingDecision(reasoning="r", action="delegate", agent="Requirements_Agent", operation="GENERATE_PRD",
                            different_product=True),
            RoutingDecision(reasoning="r", action="delegate", agent="Requirements_Agent", operation="UPDATE_PRD",
                            different_product=True),
            RoutingDecision(reasoning="r", action="respond", reply="Your PRD has been reviewed.", different_product=True)):
        corrected = apply_intent_rules(decision, store_stage(), state)
        assert corrected.action == "respond"
        assert "different product" in corrected.reply and "**New Project**" in corrected.reply
        assert corrected.reply.startswith("This chat already has a PRD")             # no quoted objective
    # explicit replacement (different_product false) and chats without a PRD are not affected
    replace = RoutingDecision(reasoning="r", action="delegate", agent="Requirements_Agent", operation="GENERATE_PRD")
    assert apply_intent_rules(replace, store_stage(), state) is replace
    first = RoutingDecision(reasoning="r", action="delegate", agent="Requirements_Agent", operation="GENERATE_PRD",
                            different_product=True)
    assert apply_intent_rules(first, None, {"project": None}) is first


def store_stage():
    from app.services import project_store as store
    return store.STAGE_PRD_REVIEW


def test_chats_run_in_parallel_but_one_chat_runs_its_messages_in_order(monkeypatch):
    import asyncio
    events = []

    async def fake(conversation_id, user_query, file=None, *, user_id):
        events.append(f"start {conversation_id}:{user_query}")
        await asyncio.sleep(0.05)
        events.append(f"end {conversation_id}:{user_query}")
        return user_query

    monkeypatch.setattr(cs, "_process_chat_request", fake)

    async def run():
        return await asyncio.gather(cs.process_chat_request("a", "1", user_id="u"),
                                    cs.process_chat_request("a", "2", user_id="u"),
                                    cs.process_chat_request("b", "3", user_id="u"))

    assert asyncio.run(run()) == ["1", "2", "3"]
    assert events.index("end a:1") < events.index("start a:2")          # same chat: one after the other
    assert events.index("start b:3") < events.index("end a:1")          # other chat: in parallel


def test_a_reply_that_does_not_show_the_saved_document_is_replaced():
    meta = "Please share the update you'd like me to present, and I'll format it as a clear user-facing message."
    assert not cs.presents_document(meta)
    shown = cs.stored_document_reply("TRD", {"project": {"prd": PRD, "trd": TRD, "trd_version": 1}})
    assert not cs.presents_document(shown) or "Technical Requirements Document" in shown
    long_doc = "Here is the suggested **Technical Requirements Document (TRD)**:\n\n" + "\n".join(
        f"- **Field {i}:** value with enough words to look like a document section" for i in range(12))
    assert cs.presents_document(long_doc)
    import inspect
    assert "presents_document(response_text)" in inspect.getsource(cs._execute_agent_pipeline)


def test_history_is_sent_in_full_within_a_token_budget():
    from agent_framework import Message
    long_message = "Build a clinic app. " + "Patients book appointments and receptionists manage schedules. " * 30
    assert cs.compact_history_message("user", long_message) == long_message          # no more 350-char cut
    assert cs.compact_history_message("assistant", "Sorry (Reference: ab12cd34)") is None   # errors still dropped
    messages = [Message(role="user", contents=["word " * 1000]) for _ in range(10)]
    kept = cs.history_within_budget(messages, budget=3500)
    assert kept == messages[-len(kept):] and 2 <= len(kept) <= 4                        # newest, in order
    assert cs.history_within_budget(messages[:3], budget=60_000) == messages[:3]        # small chats: everything
    assert cs.history_token_budget() == 60_000
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "message_list[-9:-1]" not in source and "message_list[-7:-1]" not in source
    assert "history_within_budget(to_messages(history))" in source


@pytest.mark.parametrize("query, history, expected", [
    ("make reasonable assumptions", [], True),
    ("Please write it. Make some reasonable assumptions for the rest.", [], True),
    ("yes", [{"role": "user", "text": "A clinic app ... Make reasonable assumptions for anything else."}], True),
    ("just generate it", [], True),
    ("no more questions please", [], True),
    ("Patients and doctors", [{"role": "user", "text": "A clinic app"}], False),
    ("what assumptions did you make?", [], False),
])
def test_explicit_requests_to_proceed_on_assumptions(query, history, expected):
    assert cs.asked_to_proceed(query, history) is expected


def test_prompts_ask_for_suggested_documents_and_the_closing_rule():
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "Here is the suggested **Product Requirements Document" in source
    assert "Here is the suggested **Technical Requirements Document" in source
    assert source.count("CLOSING RULE (every case)") == 2
    assert 'no "Proposed Azure DevOps Project" title' in source
    assert "do NOT mention or list any commands" in source
