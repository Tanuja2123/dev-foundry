"""Seen live: "approved, create the repo" -> repository created, reply failed ("Nothing was changed"), the same message
again -> pushed a structure the user never saw. A push now needs an explicit request about a structure that was shown,
and a failed reply after real changes reports them."""
import pytest

from app.services import conversation_service as cs
from app.services import project_store as store

FILES = [{"path": "/frontend/package.json", "content": "{}"}, {"path": "/backend/bin/www", "content": "#!/usr/bin/env node"}]


def state(structure_version=1, commit_id=None, repo_id="repo-1"):
    return {"project": {"project_name": "Clinic", "ado_project_id": "p1", "ado_project_url": "https://dev.azure.com/o/p",
                        "prd_version": 1, "trd_version": 1},
            "repository": {"repo_name": "clinic-appointments", "ado_repo_id": repo_id, "structure": FILES,
                           "structure_version": structure_version, "status": store.REPO_STRUCTURE_REVIEW,
                           "commit_id": commit_id, "pushed_structure": None, "branch": "main"}}


def shown(version):
    return {"role": "assistant",
            "text": f"**Proposed repository structure** (version {version}, 2 files)\n\n```text\n...\n```"}


@pytest.mark.parametrize("message, history, blocked", [
    # the live case: the structure was never shown and the user did not ask to push
    ("dont switch. approved, create the repo.", [{"role": "assistant", "text": "⚠️ The AI service is busy..."}], True),
    ("approved", [shown(1)], True),                                    # approval alone is not a push request
    ("push it to main", [], True),                                     # asked, but never saw the structure
    ("push it to main", [shown(1)], False),                            # asked and saw it -> push
    ("Please push the initial structure to the main branch in Azure DevOps.", [shown(1)], False),   # the chip
    ("push these changes", [shown(1)], True),                          # saw version 1, but version 2 is current
])
def test_push_needs_an_explicit_request_about_a_shown_structure(message, history, blocked):
    current = state(structure_version=2 if message == "push these changes" else 1)
    reply = cs.push_review_reply(message, history, current)
    assert (reply is not None) is blocked
    if reply:
        assert "clinic-appointments" in reply and "push to main" in reply
        assert "(version" in reply                                     # the tree, with its version (recognised next time)


def test_after_the_review_reply_the_explicit_push_goes_ahead():
    current = state()
    review = cs.push_review_reply("approved", [], current)
    history = [{"role": "assistant", "text": review}]
    assert cs.push_review_reply("push to main", history, current) is None


def test_a_failed_reply_after_real_changes_reports_them():
    before = {"project": state()["project"], "repository": None}
    after = state()
    reply = cs.partial_success_reply(before, after, "abc12345")
    assert reply.startswith("Part of your request was completed")
    assert "(Reference: abc12345)" in reply
    assert "The repository **clinic-appointments** was created." in reply
    assert "(version 1, 2 files)" in reply and "Nothing has been pushed yet" in reply
    assert "Nothing was changed" not in reply


def test_a_failed_reply_without_changes_keeps_the_usual_message():
    assert cs.partial_success_reply(state(), state(), "r") is None


def test_a_push_that_happened_before_the_reply_failed_is_reported():
    reply = cs.partial_success_reply(state(), state(commit_id="4456b940dc44"), "r")
    assert "pushed to `main` (commit `4456b94`)" in reply


def test_the_guard_is_wired_into_the_pipeline_and_prompt():
    import inspect
    source = inspect.getsource(cs._execute_agent_pipeline)
    assert "push_review_reply(user_query, history, workflow_state)" in source
    assert "partial_success_reply(before, store.get_workflow_state(conversation_id), reference)" in source
    assert "it never moves two steps" in source
