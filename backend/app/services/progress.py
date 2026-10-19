"""
Live progress of a chat turn ("Checking your message", "Writing the TRD (MAQ AI)", "Running `npx create-vite`...").

The pipeline reports what it is doing; the dashboard polls GET /api/chat/progress/{conversation_id} while it waits, so
the user sees the real step instead of a silent timer. The current conversation travels in a context variable, so code
deep down (the LLM client, generator commands in worker threads) can report without passing ids around.
Kept in memory only: one short text per running conversation, removed when the turn ends.

Stop (POST /api/chat/cancel/{conversation_id}): cancels the running turn as long as it hasn't changed anything outside
DevFoundry yet. Code that changes Azure DevOps runs inside `side_effect(...)`: from then on the turn is finished
instead (an action is never cut off halfway, and its result is saved and shown), and a Stop that came first prevents
the action from starting at all.
"""
import asyncio
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterator, Optional

_current_conversation: ContextVar[Optional[str]] = ContextVar("devfoundry_progress_conversation", default=None)
_progress: Dict[str, Dict[str, object]] = {}      # conversation id -> {"user", "step", "detail", "since", "updated",
#                                                    "task", "committed", "cancel_requested"}
_lock = threading.Lock()                          # side_effect() runs in worker threads too


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


def note(name: str, value: object) -> None:
    """Remembers a fact about the running turn for the end of it (e.g. which document was shown, for UX-9 buttons)."""
    entry = _progress.get(_current_conversation.get() or "")
    if entry is not None:
        entry.setdefault("notes", {})[name] = value


def noted(name: str, conversation_id: Optional[str] = None) -> object:
    entry = _progress.get(conversation_id or _current_conversation.get() or "") or {}
    return (entry.get("notes") or {}).get(name)


def finish(conversation_id: Optional[str]) -> None:
    if conversation_id:
        _progress.pop(conversation_id, None)


def attach_task(conversation_id: Optional[str], task: "asyncio.Task") -> None:
    """The task running the conversation's turn (what Stop cancels)."""
    entry = _progress.get(conversation_id or "")
    if entry is not None:
        entry["task"] = task


def begin_side_effect(action: str) -> None:
    """
    Call right before a change outside DevFoundry (e.g. "Creating the repository in Azure DevOps"). If Stop was
    clicked before, the action does not start (the turn is cancelled); once it starts, Stop no longer cancels the turn.
    """
    entry = _progress.get(_current_conversation.get() or "")
    if entry is not None:
        with _lock:
            if entry.get("cancel_requested"):
                raise asyncio.CancelledError()
            entry.setdefault("committed", action)


@contextmanager
def side_effect(action: str) -> Iterator[None]:
    begin_side_effect(action)
    yield


def tool_started() -> None:
    """A tool (e.g. the generator commands) started inside the current turn: the stream watchdog pauses meanwhile."""
    entry = _progress.get(_current_conversation.get() or "")
    if entry is not None:
        with _lock:
            entry["tools_running"] = int(entry.get("tools_running") or 0) + 1


def tool_finished() -> None:
    entry = _progress.get(_current_conversation.get() or "")
    if entry is not None:
        with _lock:
            entry["tools_running"] = max(0, int(entry.get("tools_running") or 0) - 1)


def tools_running(conversation_id: Optional[str] = None) -> int:
    """How many tools of the conversation (default: the current one) are running right now."""
    entry = _progress.get(conversation_id or _current_conversation.get() or "")
    return int((entry or {}).get("tools_running") or 0)


async def wait_for_tools(conversation_id: Optional[str] = None, timeout: float = 900.0) -> bool:
    """
    Waits until no tool of the conversation is running (e.g. a generator thread that outlived an abandoned model call),
    so the reply is written from the final stored state. True when nothing is running any more.
    """
    deadline = time.time() + timeout
    while tools_running(conversation_id) and time.time() < deadline:
        await asyncio.sleep(1.0)
    return not tools_running(conversation_id)


def cancel_requested(conversation_id: Optional[str] = None) -> bool:
    """True once Stop was clicked for the running turn (work still in a thread must not save its result)."""
    entry = _progress.get(conversation_id or _current_conversation.get() or "")
    return bool((entry or {}).get("cancel_requested"))


def cancel(conversation_id: str, user_id: str) -> Dict[str, object]:
    """
    Stop for the conversation's running turn (owner only):
    {"status": "cancelled"} - stopped, nothing was changed or saved;
    {"status": "finishing", "action": ...} - an Azure DevOps action already started, so the turn finishes;
    {"status": "idle"} - nothing is running.
    """
    entry = _progress.get(conversation_id)
    if not entry or entry.get("user") != user_id:
        return {"status": "idle"}
    with _lock:
        if entry.get("committed"):
            return {"status": "finishing", "action": entry["committed"]}
        entry["cancel_requested"] = True
        task = entry.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        task.get_loop().call_soon_threadsafe(task.cancel)       # the route may run in a worker thread
    entry.update(step="Stopping", detail="", updated=time.time())
    return {"status": "cancelled"}


def get(conversation_id: str, user_id: str) -> Optional[Dict[str, object]]:
    """The running step of a conversation owned by `user_id` (None when nothing is running)."""
    entry = _progress.get(conversation_id)
    if not entry or entry.get("user") != user_id:
        return None
    now = time.time()
    return {"step": entry["step"], "detail": entry["detail"], "elapsed_seconds": round(now - float(entry["since"])),
            "step_seconds": round(now - float(entry["updated"]))}
