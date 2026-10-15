"""
Live progress of a chat turn ("Checking your message", "Writing the TRD (MAQ AI)", "Running `npx create-vite`...").

The pipeline reports what it is doing; the dashboard polls GET /api/chat/progress/{conversation_id} while it waits, so
the user sees the real step instead of a silent timer. The current conversation travels in a context variable, so code
deep down (the LLM client, generator commands in worker threads) can report without passing ids around.
Kept in memory only: one short text per running conversation, removed when the turn ends.
"""
import time
from contextvars import ContextVar
from typing import Dict, Optional

_current_conversation: ContextVar[Optional[str]] = ContextVar("devfoundry_progress_conversation", default=None)
_progress: Dict[str, Dict[str, object]] = {}      # conversation id -> {"user", "step", "detail", "since", "updated"}


def start(conversation_id: Optional[str], user_id: str) -> None:
    if not conversation_id:
        return
    _current_conversation.set(conversation_id)
    now = time.time()
    _progress[conversation_id] = {"user": user_id, "step": "Checking your message", "detail": "", "since": now,
                                  "updated": now}


def step(text: str, conversation_id: Optional[str] = None) -> None:
    """The main step of the turn (resets the detail line)."""
    entry = _progress.get(conversation_id or _current_conversation.get() or "")
    if entry is not None:
        entry.update(step=text, detail="", updated=time.time())


def detail(text: str, conversation_id: Optional[str] = None) -> None:
    """A detail under the current step, e.g. which model is answering or which command runs."""
    entry = _progress.get(conversation_id or _current_conversation.get() or "")
    if entry is not None:
        entry.update(detail=text, updated=time.time())


def finish(conversation_id: Optional[str]) -> None:
    if conversation_id:
        _progress.pop(conversation_id, None)


def get(conversation_id: str, user_id: str) -> Optional[Dict[str, object]]:
    """The running step of a conversation owned by `user_id` (None when nothing is running)."""
    entry = _progress.get(conversation_id)
    if not entry or entry.get("user") != user_id:
        return None
    now = time.time()
    return {"step": entry["step"], "detail": entry["detail"], "elapsed_seconds": round(now - float(entry["since"])),
            "step_seconds": round(now - float(entry["updated"]))}
