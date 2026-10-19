"""
Buttons under an assistant reply (FEATURES UX-9): open what the turn produced, instead of copying links from the text.

Computed by code from the stored state before and after the turn (never from the model's text), so a button only
appears for something that really exists:
- "document": open the PRD / TRD in a drawer next to the chat (written or changed this turn, or asked to be shown)
- "projects": open the Projects page with this project highlighted and the document open
- "link":     the Azure DevOps project, repository, backlog or pull request (new or changed this turn)
Stored with the assistant message, so the buttons are still there when the conversation is opened again.
"""
from typing import Any, Dict, List, Optional

MAX_ACTIONS = 4
DOCUMENTS = ("PRD", "TRD")


def _project(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (state or {}).get("project") or {}


def _repository(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (state or {}).get("repository") or {}


def _document_changed(document: str, before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    key, version = document.lower(), f"{document.lower()}_version"
    if not after.get(key):
        return False
    return not before.get(key) or before.get(version) != after.get(version) or before.get(key) != after.get(key)


def _changed_review(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    old, new = before.get("pr_reviews") or {}, after.get("pr_reviews") or {}
    changed = [review for key, review in new.items() if review != old.get(key)]
    changed.sort(key=lambda review: review.get("reviewed_at") or "")
    return changed[-1] if changed else None


def reply_actions(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
                  show_document: Optional[str] = None) -> List[Dict[str, str]]:
    """The buttons for this turn's reply, most useful first (at most MAX_ACTIONS)."""
    old_project, new_project = _project(before), _project(after)
    old_repo, new_repo = _repository(before), _repository(after)
    if (before or {}).get("error") or (after or {}).get("error"):
        return []
    actions: List[Dict[str, str]] = []

    documents = [doc for doc in DOCUMENTS
                 if new_project.get(doc.lower()) and (doc == show_document
                                                      or _document_changed(doc, old_project, new_project))]
    for doc in documents:
        actions.append({"kind": "document", "document": doc, "label": f"Open {doc}"})
    if documents:
        actions.append({"kind": "projects", "document": documents[0], "label": "View in Projects"})

    project_url = new_project.get("ado_project_url")
    if project_url and new_project.get("ado_project_id") and not old_project.get("ado_project_id"):
        actions.append({"kind": "link", "label": "Azure DevOps project", "url": project_url})

    repo_url = new_repo.get("ado_repo_url")
    if repo_url and new_repo.get("ado_repo_id") and (
            new_repo.get("repo_id") != old_repo.get("repo_id") or not old_repo.get("ado_repo_id")
            or new_repo.get("commit_id") != old_repo.get("commit_id")):
        actions.append({"kind": "link", "label": "Repository", "url": repo_url})

    if project_url and new_project.get("work_items") and new_project.get("work_items") != old_project.get("work_items"):
        actions.append({"kind": "link", "label": "Backlog", "url": f"{project_url.rstrip('/')}/_backlogs/backlog"})

    review = _changed_review(old_repo, new_repo) if new_repo.get("repo_id") == old_repo.get("repo_id") else None
    if review and review.get("url"):
        actions.append({"kind": "link", "label": f"Pull request #{review.get('pr_id')}", "url": review["url"]})

    return actions[:MAX_ACTIONS]
