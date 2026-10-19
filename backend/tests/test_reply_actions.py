"""Buttons under an assistant reply (FEATURES UX-9), computed from the stored state before and after the turn."""
from app.services import progress
from app.services.reply_actions import reply_actions

PROJECT_URL = "https://dev.azure.com/org/BookNook"
REPO_URL = "https://dev.azure.com/org/BookNook/_git/booknook"


def state(project=None, repository=None):
    return {"project": project, "repository": repository}


def kinds(actions):
    return [(a["kind"], a.get("document") or a.get("label")) for a in actions]


def test_new_prd_gets_open_and_projects_buttons():
    after = state({"prd": {"Objective": "x"}, "prd_version": 1})
    actions = reply_actions(state(), after)
    assert kinds(actions) == [("document", "PRD"), ("projects", "PRD")]
    assert actions[0]["label"] == "Open PRD"


def test_updated_trd_only_gets_trd_button():
    before = state({"prd": {"a": 1}, "prd_version": 2, "trd": {"b": 1}, "trd_version": 1})
    after = state({"prd": {"a": 1}, "prd_version": 2, "trd": {"b": 2}, "trd_version": 2})
    assert kinds(reply_actions(before, after)) == [("document", "TRD"), ("projects", "TRD")]


def test_unchanged_documents_get_no_buttons():
    same = state({"prd": {"a": 1}, "prd_version": 2})
    assert reply_actions(same, same) == []


def test_shown_document_gets_a_button_without_changing():
    same = state({"prd": {"a": 1}, "prd_version": 2, "trd": {"b": 1}, "trd_version": 1})
    assert kinds(reply_actions(same, same, "TRD")) == [("document", "TRD"), ("projects", "TRD")]


def test_show_document_that_does_not_exist_gets_no_button():
    same = state({"prd": {"a": 1}, "prd_version": 2})
    assert reply_actions(same, same, "TRD") == []


def test_created_ado_project_and_repository_links():
    before = state({"prd": {"a": 1}, "prd_version": 1})
    after = state({"prd": {"a": 1}, "prd_version": 1, "ado_project_id": "p1", "ado_project_url": PROJECT_URL},
                  {"repo_id": "r1", "ado_repo_id": "g1", "ado_repo_url": REPO_URL})
    actions = reply_actions(before, after)
    assert [(a["label"], a["url"]) for a in actions] == [("Azure DevOps project", PROJECT_URL),
                                                         ("Repository", REPO_URL)]


def test_push_links_the_repository_but_not_the_existing_project():
    project = {"ado_project_id": "p1", "ado_project_url": PROJECT_URL}
    repo = {"repo_id": "r1", "ado_repo_id": "g1", "ado_repo_url": REPO_URL, "commit_id": None}
    actions = reply_actions(state(project, repo), state(project, dict(repo, commit_id="abc")))
    assert [a["label"] for a in actions] == ["Repository"]
    assert reply_actions(state(project, dict(repo, commit_id="abc")), state(project, dict(repo, commit_id="abc"))) == []


def test_created_work_items_link_the_backlog():
    project = {"ado_project_id": "p1", "ado_project_url": PROJECT_URL + "/"}
    actions = reply_actions(state(project), state(dict(project, work_items={"FR-001": {"id": 7}})))
    assert actions == [{"kind": "link", "label": "Backlog", "url": PROJECT_URL + "/_backlogs/backlog"}]


def test_reviewed_pull_request_link():
    project = {"ado_project_id": "p1", "ado_project_url": PROJECT_URL}
    repo = {"repo_id": "r1", "ado_repo_id": "g1", "ado_repo_url": REPO_URL, "pr_reviews": {}}
    review = {"pr_id": 4, "url": REPO_URL + "/pullrequest/4", "reviewed_at": "2026-10-16T10:00:00"}
    after = state(project, dict(repo, pr_reviews={"4": review}))
    assert reply_actions(state(project, repo), after) == [
        {"kind": "link", "label": "Pull request #4", "url": REPO_URL + "/pullrequest/4"}]
    posted = state(project, dict(repo, pr_reviews={"4": dict(review, posted={"f1": 11})}))
    assert [a["label"] for a in reply_actions(after, posted)] == ["Pull request #4"]
    assert reply_actions(posted, posted) == []


def test_state_errors_and_missing_state_give_no_buttons():
    assert reply_actions(None, None) == []
    assert reply_actions(state(), {"project": None, "repository": None, "error": "db down"}) == []


def test_at_most_four_buttons():
    after = state({"prd": {"a": 1}, "prd_version": 1, "trd": {"b": 1}, "trd_version": 1, "ado_project_id": "p1",
                   "ado_project_url": PROJECT_URL}, {"repo_id": "r1", "ado_repo_id": "g1", "ado_repo_url": REPO_URL})
    assert len(reply_actions(state(), after)) == 4


def test_progress_notes_live_only_during_the_turn():
    progress.start("conv-notes", "user-1")
    progress.note("show_document", "PRD")
    assert progress.noted("show_document", "conv-notes") == "PRD"
    progress.finish("conv-notes")
    assert progress.noted("show_document", "conv-notes") is None


def test_pipeline_saves_and_returns_the_buttons(monkeypatch):
    """A turn that writes the PRD: the reply carries Open PRD / View in Projects, and they are stored with it."""
    import asyncio
    from types import SimpleNamespace
    from app.services import conversation_service as cs

    states = iter([state(), state({"prd": {"Objective": "x"}, "prd_version": 1})])
    saved = {}

    class Table:
        def update(self, fields):
            saved.update(fields)
            return self

        def eq(self, *_):
            return self

        def execute(self):
            return None

    async def allow(text):
        return SimpleNamespace(allowed=True, response_text=None)

    async def generate(user_query, content, conversation_id):
        progress.note("show_document", None)
        return "Here is the suggested PRD.\n\n---\n\nTell me what to change."

    monkeypatch.setattr(cs, "get_conversation_by_id", lambda *_: {"conv_id": "c1", "title": "Books", "content": []})
    monkeypatch.setattr(cs, "check_input_guardrail", allow)
    monkeypatch.setattr(cs, "check_output_guardrail", allow)
    monkeypatch.setattr(cs, "generate_chat_response", generate)
    monkeypatch.setattr(cs.store, "get_workflow_state", lambda _id: next(states))
    monkeypatch.setattr(cs.store, "get_project", lambda _id: None)
    monkeypatch.setattr(cs, "get_supabase_client", lambda: SimpleNamespace(table=lambda _name: Table()))
    monkeypatch.setattr(cs, "get_suggestions", lambda *_: [])

    response = asyncio.run(cs.process_chat_request("c1", "write the PRD", user_id="u1"))
    labels = [a.label for a in response.actions]
    assert labels == ["Open PRD", "View in Projects"]
    assistant = saved["content"][-1]
    assert assistant["role"] == "assistant" and [a["label"] for a in assistant["actions"]] == labels
