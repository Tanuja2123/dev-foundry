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


# ---------------------------------------------------------------------------- REV-7 / SEC-17

def test_version_numbers_are_shielded_from_the_pii_filter():
    from app.services import conversation_service as cs
    text = "```bash\nnpx create-next-app@latest frontend    # create-next-app 16.0.10\nmix phx.new . # Mix 1.18.2-otp-27\n```"
    shielded, found = cs.shield_versions(text)
    assert "16.0.10" not in shielded and "1.18.2-otp-27" not in shielded and len(found) == 2
    assert cs.restore_versions(shielded, found) == text
    assert cs.shield_versions("Call 040 2345 6789 or v2 of the API")[1] == {}      # only dotted versions


def test_a_redaction_is_mentioned_once():
    from app.services import conversation_service as cs
    before = "Support contact: support@clinic.example\n\n---\n\nShall I create the TRD?"
    after = "Support contact: <EMAIL_ADDRESS>\n\n---\n\nShall I create the TRD?"
    note = cs.redaction_note(before, after)
    assert note.startswith("Note: for privacy I replaced an email address with a placeholder such as `<EMAIL_ADDRESS>`")
    reply = cs.insert_before_closing(after, note)
    assert reply.endswith("Shall I create the TRD?") and reply.index("Note: for privacy") < reply.index("---")
    assert cs.redaction_note(after, after) is None                 # already a placeholder before the check


def test_every_target_role_needs_a_requirement():
    from app.services import conversation_service as cs
    prd = {"Target Users": ["Patients - book appointments", "Receptionists - manage schedules",
                            "Doctors - see their own day"],
           "Functional Requirements": [
               {"id": "FR-001", "requirement": "The system shall let patients book an appointment."},
               {"id": "FR-002", "requirement": "The system shall let a receptionist edit a doctor's schedule."}]}
    note = cs.role_coverage_note(prd)
    assert note.startswith("Note: **Doctors** is a target user, but no requirement says what they do")
    prd["Functional Requirements"].append({"id": "FR-003", "requirement": "Doctors shall see their appointments for the day."})
    assert cs.role_coverage_note(prd) is None
    assert cs.role_coverage_note({"Target Users": ["Admin (IT staff)"], "Functional Requirements": [
        {"requirement": "The admin shall manage user accounts."}]}) is None


def test_requirements_prompt_covers_roles_jurisdiction_and_compliance_wording():
    from app.services.agents import requirements_agent as ra
    import inspect
    prompt = inspect.getsource(ra)
    assert "I6. Jurisdiction - ONLY for regulated domains" in prompt and "never pick a regime yourself" in prompt
    assert "must be the actor of at least one Functional Requirement" in prompt
    assert "Assumed role:" in prompt
    assert "Designed to support <regime>; needs a compliance" in prompt and "never\n    \"<regime>-compliant\"" in prompt


def test_routing_sees_recent_turns_shortened_with_the_last_question_kept():
    from agent_framework import Message
    from app.services import conversation_service as cs
    long_prd = "PRD section. " * 400 + "Shall I create the TRD now?"
    history = [Message(role="user" if n % 2 == 0 else "assistant", contents=[f"message {n}"]) for n in range(12)]
    history.append(Message(role="assistant", contents=[long_prd]))
    routed = cs.routing_history(history)
    assert len(routed) == cs.ROUTING_HISTORY_MESSAGES
    assert routed[0].text == "message 5" and routed[-1].text.endswith("Shall I create the TRD now?")
    assert len(routed[-1].text) <= cs.ROUTING_MESSAGE_CHARS + 10
    import inspect
    assert "routing_history(message_list[:-1])" in inspect.getsource(cs._execute_agent_pipeline)
    # the Repo Setup Agent gets a compacted history too (AGT-46: a structure edit was ~21.5k tokens)
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "routing_history(message_list[:-1], keep=REPO_SETUP_HISTORY_MESSAGES" in source
    long_tree = "gym-pass/\n" + "│   ├── file.txt\n" * 400
    compact = cs.routing_history([Message(role="assistant", contents=[long_tree])] * 20,
                                 keep=cs.REPO_SETUP_HISTORY_MESSAGES, max_chars=cs.REPO_SETUP_MESSAGE_CHARS)
    assert len(compact) == 12 and all(len(m.text) <= cs.REPO_SETUP_MESSAGE_CHARS + 10 for m in compact)


# ---------------------------------------------------------------------------- LLM-11 / REV-3 / REV-4: code-rendered

def _req_state(prd_version=1, trd=None, trd_version=0, trd_prd_version=0):
    prd = {"Objective": "Clinic booking.", "Target Users": ["Patients - book"],
           "Functional Requirements": [{"id": "FR-001", "requirement": "Patients shall book a slot."}],
           "Assumptions": ["Email sign-in", "One clinic location"]}
    return {"project": {"prd": prd, "prd_version": prd_version, "trd": trd, "trd_version": trd_version,
                        "trd_prd_version": trd_prd_version, "workflow_stage": "PRD_REVIEW"}, "repository": None}


def test_clarifications_show_progress_and_numbered_questions():
    from app.services import conversation_service as cs
    data = {"status": "CLARIFICATION_REQUIRED", "workflow_stage": "PRD", "known_so_far": "A clinic booking app.",
            "readiness": {"R1": "provided", "R2": "provided", "R3": "missing", "R4": "assumed", "R5": "missing"},
            "questions": [{"question": "What must receptionists be able to do?", "choices": ["Manage schedules", "Other"]},
                          {"question": "How do you measure success?", "choices": []}]}
    reply = cs.requirements_reply("GENERATE_PRD", data, {}, {})
    assert "**What I know so far:** A clinic booking app. (**3 of 5** essentials known.)" in reply
    assert "1. **What must receptionists be able to do?**\n   - Manage schedules" in reply
    assert "say **make reasonable assumptions**" in reply and reply.count("\n---\n") == 1
    assert cs.question_chips(reply)[0]["prompt"] == "make reasonable assumptions"          # the chip (UX-10)
    blocked = {"status": "CLARIFICATION_REQUIRED", "known_so_far": cs._GUARDRAIL_KNOWN,
               "clarification_questions": [{"question": "I paused this message...", "choices": []}]}
    assert cs.requirements_reply("UPDATE_PRD", blocked, {}, {}) == "I paused this message..."


def test_a_new_prd_is_the_saved_document_with_numbered_assumptions():
    from app.services import conversation_service as cs
    reply = cs.requirements_reply("GENERATE_PRD", {"status": "COMPLETE"}, {}, _req_state())
    assert reply.startswith("Here is the suggested **Product Requirements Document (PRD)** (version 1):")
    assert "## Assumptions\n\n1. Email sign-in\n2. One clinic location" in reply
    assert "To change an assumption, say \"change assumption 2 to ...\"" in reply


def test_an_update_shows_only_what_changed_and_what_is_now_outdated():
    from app.services import conversation_service as cs
    before = _req_state(prd_version=2, trd={"Backend Technology Stack": {}}, trd_version=1, trd_prd_version=2)
    after = _req_state(prd_version=3, trd={"Backend Technology Stack": {}}, trd_version=1, trd_prd_version=2)
    data = {"status": "COMPLETE", "change_notes": "FR-008: added the waiting list\nUser Flow: added the waiting step"}
    reply = cs.requirements_reply("UPDATE_PRD", data, before, after)
    assert reply.startswith("The PRD was updated (version 2 → 3). What changed:\n\n- FR-008: added the waiting list")
    assert "Now out of date because of this change:" in reply and "## Functional Requirements" not in reply
    assert reply.endswith("Say **show me the PRD** to see the whole document, or tell me what else to change.")
    same = cs.requirements_reply("UPDATE_TRD", {"status": "COMPLETE", "change_notes": "Redis is already in the TRD"},
                                 before, before)
    assert same.startswith("Nothing changed in the TRD: Redis is already in the TRD")
    assert cs.requirements_reply("UPDATE_TRD", {"status": "CONFIRMATION_REQUIRED"}, before, after) is None


# ---------------------------------------------------------------------------- AGT-49: the push reply is built by code

def test_push_reply_reports_the_real_commits():
    after = {"repository": {"repo_name": "gym-pass", "branch": "main", "scaffold_commands": [
        {"command": "npx --yes create-next-app@latest frontend --ts --skip-install"},
        {"command": "npx --yes express-generator@latest --no-view backend"}]}}
    result = {"status": "success", "branch": "main", "files_count": 26, "commits": [
        {"kind": "generator output", "commit_id": "6a9b1c78d650", "files_count": 24,
         "commit_message": "chore: initial scaffold from generator commands\n\nGenerated with: ...",
         "commit_url": "https://dev.azure.com/o/p/_git/gym-pass/commit/6a9b1c78d650"},
        {"kind": "requested changes", "commit_id": "eaf720732274", "files_count": 26,
         "commit_message": "docs: add README.md and docs/architecture.md\n\nRequested in DevFoundry: 2 added",
         "changes": {"add": ["/README.md", "/docs/architecture.md"], "edit": [], "delete": []},
         "commit_url": "https://dev.azure.com/o/p/_git/gym-pass/commit/eaf720732274"}]}
    text = cs.push_reply(result, after)
    assert text.startswith("Pushed to **main** of **gym-pass** in 2 commits:")
    assert "1. `6a9b1c78` chore: initial scaffold from generator commands: the generator output (24 files)" in text
    assert "2. `eaf72073` docs: add README.md and docs/architecture.md: 2 added" in text   # not "26 files"
    assert "```bash\nnpx --yes create-next-app@latest frontend --ts --skip-install" in text
    assert "├──" not in text                                                       # no tree again
    assert text.count("---") == 1 and text.rstrip().endswith("or tell me what to change next.")
    follow_up = {"status": "success", "branch": "main", "commits": [
        {"kind": "follow-up commit", "commit_id": "1234abcd99", "commit_message": "chore: remove frontend/test",
         "changes": {"add": [], "edit": ["/a"], "delete": ["/b", "/c"]}}]}
    text = cs.push_reply(follow_up, after)
    assert "in 1 commit:" in text and "1 changed, 2 removed" in text and "```bash" not in text


def test_push_reply_for_already_pushed_and_failures():
    after = {"repository": {"repo_name": "gym-pass", "branch": "main"}}
    text = cs.push_reply({"status": "success", "already_pushed": True, "commit_id": "eaf720732274"}, after)
    assert text.startswith("**gym-pass** is already up to date on **main** (commit `eaf72073`)")
    text = cs.push_reply({"status": "error", "message": "Permission denied."}, after)
    assert text.startswith("The push failed: Permission denied. Nothing was pushed.")
    partial = {"status": "error", "message": "timeout", "generator_commit": {
        "kind": "generator output", "commit_id": "6a9b1c78", "files_count": 24, "commit_message": "chore: x"}}
    assert "The generator output was pushed to **main**" in cs.push_reply(partial, after)


# ---------------------------------------------------------------------------- AGT-44: Orchestrator replies

def test_explicit_setup_requests_are_recognised():
    for text in ("Let's proceed with repository setup.", "The TRD looks good. Let's proceed with repository setup.",
                 "The TRD architecture and technology stack are approved. Let's proceed with project setup and propose "
                 "the Azure DevOps project name and description.", "go ahead with the Azure DevOps setup",
                 "set up the project"):
        assert cs.is_setup_request(text), text
    for text in ("Not yet, I want to review the TRD first.", "no, don't proceed with repository setup yet",
                 "what is repository setup?", "The TRD looks good."):
        assert not cs.is_setup_request(text), text


def test_plain_approvals_after_a_review():
    for text in ("approved", "Approved.", "looks good", "LGTM", "ok thanks", "yes"):
        assert cs._PLAIN_APPROVAL.match(text), text
    for text in ("approved, post them", "review PR #6 again", "looks good but check the env file"):
        assert not cs._PLAIN_APPROVAL.match(text), text


def test_project_created_reply_is_built_from_the_stored_state():
    before = {"project": {"project_name": "GymPass-1910-0854"}}
    after = {"project": {"project_name": "GymPass-1910-0854", "ado_project_id": "6908c13b-bd59",
                         "ado_project_url": "https://dev.azure.com/org/GymPass-1910-0854"},
             "repository": {"repo_name": "gym-pass", "status": "PROPOSED"}}
    text = cs.project_created_reply(before, after)
    assert text.startswith("The Azure DevOps project **[GymPass-1910-0854](https://dev.azure.com/org/GymPass-1910-0854)** was created.")
    assert "Suggested repository name: **gym-pass**." in text and "6908c13b" not in text      # no raw GUID
    assert text.rstrip().endswith("or tell me a different name.") and text.count("---") == 1
    assert cs.project_created_reply(after, after) is None                    # nothing new this turn


def test_celebratory_emoji_are_removed_but_warnings_stay():
    text = cs.normalize_reply("✅ The security review for PR #6 is ready.\n\n⚠️ Something is off\n🎉 Done")
    assert text.startswith("The security review") and "⚠️ Something is off" in text and "\nDone" in text


# ---------------------------------------------------------------------------- AGT-42: document rendering

def test_empty_sections_are_said_and_the_trd_reads_stack_first():
    from app.utils.document_markdown import prd_json_to_markdown, trd_json_to_markdown
    prd = prd_json_to_markdown({"Objective": "Gym booking", "Non-Goals": []})
    assert "## Non-Goals\n\nNone stated." in prd
    trd = trd_json_to_markdown({"Security": {"Compliance": "Not specified"},
                                "Backend Technology Stack": {"Framework": "Express"},
                                "Integration & Infrastructure": {"External Integrations": []},
                                "Frontend Technology Stack": {"Framework": "Next.js"}})
    order = [line for line in trd.splitlines() if line.startswith("## ")]
    assert order == ["## Frontend Technology Stack", "## Backend Technology Stack", "## Integration & Infrastructure",
                     "## Security"]
    assert "**External Integrations:** None stated." in trd


# ---------------------------------------------------------------------------- AGT-40 / AGT-41: update replies

def test_change_notes_are_short_plain_bullets():
    long_note = ("Assumptions: replaced assumption 2 (fixed class capacity) with the payment rule. Non-Goals: added online "
                 "payment as a non-goal. FR-001: removed 'remaining capacity' from the class list display because the "
                 "capacity assumption was removed. FR-003: removed because it existed solely to support that assumption.")
    notes = cs.readable_change_notes(long_note)
    assert len(notes) == 4 and notes[0].startswith("Assumptions: replaced assumption 2")
    trd = cs.readable_change_notes("Database Technology Stack.database: PostgreSQL -> MySQL (user requested MySQL). "
                                   "Integration & Infrastructure.deployment: managed PostgreSQL -> managed MySQL.")
    assert trd == ["Database Technology Stack (database): PostgreSQL → MySQL (user requested MySQL). "
                   "Integration & Infrastructure (deployment): managed PostgreSQL → managed MySQL."]
    assert cs.readable_change_notes(["- FR-016: added class capacity", ""]) == ["FR-016: added class capacity"]


def test_a_question_about_a_change_is_not_the_readiness_questionnaire():
    data = {"status": "CLARIFICATION_REQUIRED", "workflow_stage": "PRD", "readiness": {"R1": "provided"},
            "known_so_far": "Assumption 2 is about class capacity, not payments.",
            "questions": [{"question": "Replace assumption 2 or add a new assumption?",
                           "choices": ["Replace assumption 2", "Add a new assumption"]}]}
    text = cs.requirements_reply("UPDATE_PRD", data, {}, {})
    assert text.startswith("Before I change the PRD, I need to check one thing. Nothing has been changed yet.")
    assert "essentials known" not in text and "make reasonable assumptions" not in text
    assert "**Replace assumption 2 or add a new assumption?**" in text and "   - Add a new assumption" in text
    assert text.rstrip().endswith("Answer and I'll update the PRD.")
    first = cs.requirements_reply("GENERATE_PRD", data, {}, {})               # a new PRD keeps the questionnaire
    assert first.startswith("Before I can write the PRD")
