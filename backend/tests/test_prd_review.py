"""Architect review of the PRD (ISSUES FE-11, D6): typed answer, cards by priority rendered by code, Add to PRD chips."""
import asyncio
from types import SimpleNamespace

from app.services import prd_review
from app.services import project_store as store
from app.services.prd_review import PrdReview, ReviewItem, clean_review, render_review, review_cards, review_chips
from app.services.workflow_routing import allowed_operations, question_chips


def item(priority, area, suggestion, finding="Not specified.", why="Rework later."):
    return ReviewItem(priority=priority, area=area, finding=finding, suggestion=suggestion, why=why)


REVIEW = PrdReview(summary="Solid core flows; access rules and data retention are missing.", items=[
    item("optional", "Dark mode", "Users can switch between light and dark themes."),
    item("must", "Roles and permissions", "Only admins can delete a book; members edit only their own reviews."),
    item("should", "Notifications", "Members get an email when a reserved book is available."),
    item("must", "Authentication", "Users sign in with email and password; sessions expire after 30 minutes."),
])


def test_clean_review_orders_dedupes_and_limits():
    noisy = PrdReview(items=REVIEW.items + [
        item("must", "Auth again", "  Users sign in with email and password; sessions expire after 30 minutes. "),
        item("should", "", "No area"), item("should", "Empty", "  ")]
        + [item("optional", f"Extra {n}", f"Optional thing {n}.") for n in range(5)])
    cleaned = clean_review(noisy)
    assert [i.priority for i in cleaned.items][:4] == ["must", "must", "should", "optional"]
    assert [i.area for i in cleaned.items if i.priority == "must"] == ["Roles and permissions", "Authentication"]
    assert sum(1 for i in cleaned.items if i.priority == "optional") == prd_review.LIMITS["optional"]
    assert all(i.area and i.suggestion for i in cleaned.items)


def test_render_review_groups_cards_by_priority():
    text = render_review(clean_review(REVIEW), 3)
    assert text.startswith(f"Here is a {prd_review.REVIEW_MARKER} (version 3): **2 must add**, **1 should consider**, "
                           "**1 optional**.")
    assert text.index("### Must add") < text.index("### Should consider") < text.index("### Optional")
    assert "> **1. Roles and permissions**" in text and "> **4. Dark mode**" in text
    assert "> **Add to the PRD:** Members get an email when a reserved book is available." in text
    assert "> *Why:* Rework later." in text
    # every card is its own quote block (separated by a blank line), and the reply ends with one closing line
    assert "\n\n> **2. Authentication**" in text
    assert text.count("---") == 1 and text.rstrip().endswith("or use the buttons below.")
    assert "Nothing was changed." in text


def test_render_review_without_items():
    text = render_review(PrdReview(summary="Complete for an MVP."), 1)
    assert "no gaps worth adding were found. Complete for an MVP." in text and "###" not in text


def test_review_cards_and_add_to_prd_chips():
    text = render_review(clean_review(REVIEW), 2)
    cards = review_cards(text)
    assert [(c[0], c[1], c[2]) for c in cards] == [(1, "Must add", "Roles and permissions"),
                                                   (2, "Must add", "Authentication"),
                                                   (3, "Should consider", "Notifications"), (4, "Optional", "Dark mode")]
    chips = review_chips(text)
    labels = [label for label, _ in chips]
    assert labels == ["Add the 2 must-add items to the PRD", "Add Notifications to the PRD", "Add Dark mode to the PRD",
                      "Keep the PRD as it is"]
    assert chips[0][1] == ("Update the PRD with these architect review items (change the named FRs instead of adding "
                           "duplicates; one testable rule per FR):\n- Add: Only admins can delete a book; members edit "
                           "only their own reviews.\n- Add: Users sign in with email and password; sessions expire after "
                           "30 minutes.")
    assert chips[1][1] == ("Add this requirement to the PRD (architect review): Members get an email when a reserved book "
                           "is available.")
    # the chips answer the review (UX-10), through the normal chip mechanism
    assert [c["label"] for c in question_chips(text)] == labels


def test_single_must_item_gets_its_own_chip():
    text = render_review(clean_review(PrdReview(items=[item("must", "Audit log", "Every deletion is logged.")])), 1)
    assert [label for label, _ in review_chips(text)] == ["Add Audit log to the PRD", "Keep the PRD as it is"]


def test_review_request_detection():
    for text in ("Act as a senior architect and review the PRD and suggest what is missing", "review the PRD",
                 "Can you review my PRD?", "Please review the requirements for security gaps",
                 "what is missing in the PRD?"):
        assert prd_review.is_review_request(text), text
    for text in ("Not yet, I want to review the PRD first.", "I will review the PRD later", "review PR #3",
                 "Add these requirements to the PRD: x", "create the TRD"):
        assert not prd_review.is_review_request(text), text


def test_review_is_allowed_only_with_a_prd():
    with_prd = {"project": {"prd": {"Objective": "x"}, "workflow_stage": store.STAGE_PRD_REVIEW}, "repository": None}
    assert "REVIEW_PRD" in allowed_operations(store.STAGE_PRD_REVIEW, with_prd)
    assert "REVIEW_PRD" in allowed_operations(store.STAGE_STRUCTURE_REVIEW, dict(with_prd))
    assert "REVIEW_PRD" not in allowed_operations(store.STAGE_REQUIREMENTS, {"project": None, "repository": None})


class FakeAgent:
    answers = []
    calls = 0

    def __init__(self, **kwargs):
        self.instructions = kwargs.get("instructions")

    async def run(self, messages, options=None):
        FakeAgent.calls += 1
        FakeAgent.last_request = messages[0].text
        answer = FakeAgent.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(value=None, messages=[SimpleNamespace(text=answer)])


def run_review(monkeypatch, answers, project):
    FakeAgent.answers, FakeAgent.calls = list(answers), 0
    monkeypatch.setattr(prd_review, "Agent", FakeAgent)
    return asyncio.run(prd_review.review_prd(object(), "review the PRD", project))


def test_review_prd_renders_the_typed_answer(monkeypatch):
    answer = REVIEW.model_dump_json()
    text = run_review(monkeypatch, ["not json", answer], {"prd": {"Objective": "Books"}, "prd_version": 4})
    assert FakeAgent.calls == 2 and "(version 4)" in text and "### Must add" in text


def test_review_prd_failures_and_missing_prd(monkeypatch):
    assert run_review(monkeypatch, [RuntimeError("busy"), "nope"], {"prd": {"a": 1}}) == prd_review.FAILED_MESSAGE
    assert run_review(monkeypatch, [], {"prd": None}) == prd_review.NO_PRD_MESSAGE and FakeAgent.calls == 0


def test_an_item_that_extends_an_existing_requirement_says_so(monkeypatch):
    # seen (GymPass): sign-in and role rules suggested again although FR-012 / FR-013 had them -> duplicate FRs
    review = PrdReview(items=[
        ReviewItem(priority="must", area="Roles", finding="Permissions are vague.", extends="fr-013",
                   suggestion="Members manage only their own bookings; trainers only their own classes."),
        ReviewItem(priority="must", area="Capacity", finding="No limit.", extends="FR-099",
                   suggestion="Each class has a maximum capacity set by the owner.")])
    cleaned = clean_review(review, {"FR-012", "FR-013"})
    assert [i.extends for i in cleaned.items] == ["FR-013", ""]                  # unknown ids are dropped
    text = render_review(cleaned, 3)
    assert "> **Change FR-013 to:** Members manage only their own bookings" in text
    assert "> **Add to the PRD:** Each class has a maximum capacity" in text
    cards = review_cards(text)
    assert [(c[2], c[4]) for c in cards] == [("Roles", "FR-013"), ("Capacity", "")]
    chips = dict(review_chips(text))
    assert "- Change FR-013 to: Members manage only their own bookings" in chips["Add the 2 must-add items to the PRD"]


def test_the_review_sees_the_requirement_list(monkeypatch):
    prd = {"Functional Requirements": [{"id": "FR-012", "requirement": "Users sign in."}]}
    run_review(monkeypatch, [REVIEW.model_dump_json()], {"prd": prd, "prd_version": 3})
    assert "functional_requirements: [{\"id\": \"FR-012\"" in FakeAgent.last_request
