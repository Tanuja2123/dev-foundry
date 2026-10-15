"""Technology-change fit check: a dedicated step before every TRD update, threshold and question enforced by code."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import conversation_service as cs
from app.services import tech_fit
from app.services.tech_fit import TechFitAssessment

STATE = {"project": {"prd": {"Objective": "Clinic booking web app"},
                     "trd": {"Backend Technology Stack": {"language": "Node.js", "framework": "Express"}}}}
COBOL = TechFitAssessment(changes_technology=True, area="Backend language and framework",
                          current_choice="Node.js / Express", requested_choice="COBOL", current_fit=8, requested_fit=2,
                          pros=["Very stable runtime"], cons=["No modern web framework", "Hard to hire for"],
                          recommendation="Keep Node.js / Express: it shares the frontend's ecosystem.")


def run_gate(monkeypatch, assessment, query="use cobol for the current backend", history=None, state=STATE):
    async def fake_assess(client, user_query, hist, prd, trd):
        return assessment
    monkeypatch.setattr(tech_fit, "assess", fake_assess)
    return asyncio.run(tech_fit.gate(None, query, history or [], state))


def test_a_much_worse_choice_gets_the_confirmation_question(monkeypatch):
    reply, confirmed = run_gate(monkeypatch, COBOL)
    assert confirmed is None
    assert "**COBOL** fits this project less well than the current **Node.js / Express**" in reply
    assert tech_fit.CONFIRMATION_MARKER in reply
    assert "**Fit for this project:** Node.js / Express 8/10 · COBOL 2/10" in reply
    assert "**Pros of COBOL:**\n- Very stable runtime" in reply and "**Cons of COBOL:**" in reply
    assert "**Recommendation:** Keep Node.js" in reply
    assert reply.endswith('Do you still want to switch to COBOL (say "yes, switch anyway"), or keep Node.js / Express?')
    assert cs.enforce_closing_line(reply) == reply                     # already in the house format


@pytest.mark.parametrize("assessment", [
    COBOL.model_copy(update={"requested_fit": 7}),                      # gap 1 < 3
    COBOL.model_copy(update={"changes_technology": False}),             # re-states / keeps the current choice
])
def test_good_fits_and_non_changes_are_applied_without_a_question(monkeypatch, assessment):
    assert run_gate(monkeypatch, assessment) == (None, None)


def test_a_reasonable_addition_is_not_questioned(monkeypatch):
    # seen live: adding Redis as a cache was questioned as "no cache 9 vs Redis 5"
    redis = TechFitAssessment(changes_technology=True, change_kind="add", area="Caching",
                              current_choice="No dedicated cache", requested_choice="Redis", current_fit=9,
                              requested_fit=5, pros=["Faster slot lookups"], cons=["One more service"],
                              recommendation="Fine to add.")
    assert run_gate(monkeypatch, redis) == (None, None)


def test_a_poor_addition_is_questioned_with_its_own_wording(monkeypatch):
    blockchain = TechFitAssessment(changes_technology=True, change_kind="add", area="Data storage",
                                   current_choice="PostgreSQL", requested_choice="a blockchain ledger", current_fit=9,
                                   requested_fit=2, pros=["Tamper evidence"], cons=["Slow", "Complex"],
                                   recommendation="Not needed for appointment bookings.")
    reply, _ = run_gate(monkeypatch, blockchain)
    assert "adding **a blockchain ledger** doesn't fit this project well" in reply
    assert "**Fit for this project:** adding a blockchain ledger 2/10" in reply
    assert reply.endswith('Do you still want to go ahead with it (say "yes, switch anyway"), or leave the TRD as it is?')
    keep = blockchain.model_copy(update={"change_kind": "keep"})
    assert run_gate(monkeypatch, keep) == (None, None)


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("TECH_CHANGE_CONFIRM_THRESHOLD", "7")
    assert run_gate(monkeypatch, COBOL) == (None, None)                # gap 6 < 7
    monkeypatch.setenv("TECH_CHANGE_CONFIRM_THRESHOLD", "2")
    assert run_gate(monkeypatch, COBOL.model_copy(update={"requested_fit": 5}))[0]   # gap 3 >= 2


def test_confirmation_applies_the_original_request(monkeypatch):
    question, _ = run_gate(monkeypatch, COBOL)
    history = [{"role": "user", "text": "use cobol for the current backend"}, {"role": "assistant", "text": question}]
    confirm = COBOL.model_copy(update={"confirms_previous_warning": True})
    assert run_gate(monkeypatch, confirm, "yes, switch anyway", history) == (None, "use cobol for the current backend")


def test_a_different_technology_after_the_warning_is_checked_again(monkeypatch):
    question, _ = run_gate(monkeypatch, COBOL)
    history = [{"role": "user", "text": "use cobol"}, {"role": "assistant", "text": question}]
    fortran = COBOL.model_copy(update={"requested_choice": "Fortran", "requested_fit": 1})
    reply, confirmed = run_gate(monkeypatch, fortran, "ok then use Fortran", history)
    assert confirmed is None and "**Fortran** fits this project less well" in reply


def test_when_the_check_cannot_run_the_user_is_still_asked(monkeypatch):
    reply, confirmed = run_gate(monkeypatch, None)
    assert confirmed is None and tech_fit.CONFIRMATION_MARKER in reply and "yes, switch anyway" in reply
    history = [{"role": "user", "text": "use cobol"}, {"role": "assistant", "text": reply}]
    assert run_gate(monkeypatch, None, "yes", history) == (None, "use cobol")


def test_no_trd_no_check(monkeypatch):
    assert run_gate(monkeypatch, COBOL, state={"project": {"prd": {}}}) == (None, None)


def test_assessment_is_parsed_from_typed_output_or_text():
    typed = SimpleNamespace(value=COBOL, messages=[])
    assert tech_fit._parse(typed) is COBOL
    text = SimpleNamespace(value=None, messages=[SimpleNamespace(text="Here:\n```json\n" + COBOL.model_dump_json() + "\n```")])
    assert tech_fit._parse(text).requested_choice == "COBOL"
    assert tech_fit._parse(SimpleNamespace(value=None, messages=[SimpleNamespace(text="no json")])) is None
    assert tech_fit.gap(COBOL.model_copy(update={"current_fit": 80, "requested_fit": 20})) == 6   # 0-100 scale


def test_affirmative_answers():
    for text in ("yes", "Yes, switch anyway", "ok", "go ahead", "switch anyway", "sure, do it"):
        assert tech_fit.looks_affirmative(text), text
    for text in ("no", "keep the current one", "use Java instead"):
        assert not tech_fit.looks_affirmative(text), text


def test_only_saved_documents_are_shown(monkeypatch):
    assert cs.persist_requirements_output(None, json.dumps({"status": "COMPLETE"})) is False
    assert cs.persist_requirements_output("c", "not json") is False
    assert cs.persist_requirements_output("c", json.dumps({"status": "CLARIFICATION_REQUIRED"})) is False
    assert "nothing has changed" in cs.document_not_saved_message("UPDATE_TRD")
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "tech_fit.gate(" in source and "unsaved_document" in source
    assert "Confirmation of the technology change -> UPDATE_TRD" in source
