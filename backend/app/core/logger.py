"""
Logging for the backend: readable, timestamped, colour-coded console output plus a plain rotating log file.

Console: "14:23:05 INFO    [Orchestrator] Completed in 2985ms ..." - the time is dim, WARNING is yellow, ERROR bold red,
and the leading [Tag] is coloured by component so a long agent run is easy to follow. Guardrail tripwires and blocks are
bold red. Colours are plain ANSI codes (no extra dependency); on Windows the console's ANSI mode is switched on, and
colours are skipped when the output is not a terminal (or NO_COLOR is set; LOG_COLOR=1 forces them).

File: backend/logs/devfoundry.log (10 MB x 3 backups, no colours, full dates) for searching. LOG_TO_FILE=0 turns it off.

Uvicorn's own loggers use the same handlers (so every line has a time), and its access log drops the polling noise
(/api/chat/progress/... every 2 s while a chat runs, /health).
"""
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import List, Tuple

LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "devfoundry.log")
QUIET_ACCESS_PATHS = ("/api/chat/progress/", "/health")

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = (f"\033[3{n}m" for n in range(1, 8))
BRIGHT_RED, BRIGHT_GREEN, BRIGHT_YELLOW, BRIGHT_BLUE, BRIGHT_MAGENTA, BRIGHT_CYAN = (
    f"\033[9{n}m" for n in (1, 2, 3, 4, 5, 6))

LEVEL_COLORS = {
    logging.DEBUG: DIM,
    logging.INFO: DIM,                      # unobtrusive
    logging.WARNING: YELLOW,
    logging.ERROR: BOLD + RED,
    logging.CRITICAL: BOLD + RED,
}

# Tag families, matched by prefix (first match wins): "[Orchestrator Guardrail Block]" is red, "[Orchestrator]" magenta
TAG_COLORS: List[Tuple[str, str]] = [
    ("Guardrail Tripwire", BOLD + RED), ("Security Alert", BOLD + RED),
    ("User Query", BOLD + BRIGHT_CYAN),
    ("Orchestrator Guardrail Block", BOLD + RED), ("Requirements_Agent Guardrail Block", BOLD + RED),
    ("Orchestrator", MAGENTA), ("Routing", MAGENTA), ("Title", MAGENTA), ("Suggestions", MAGENTA),
    ("Requirements_Agent", BLUE), ("Readiness Guard", BLUE), ("JSON Guard", BLUE), ("Tech Change Check", BLUE),
    ("Repo_Setup_Agent", CYAN), ("Scaffold", CYAN), ("ADO MCP", CYAN), ("ADO Push", CYAN), ("ADO Config", CYAN),
    ("Backlog_Agent", GREEN), ("ADO Boards", GREEN),
    ("PR Review", BRIGHT_MAGENTA), ("ADO PR", BRIGHT_MAGENTA), ("Security_Review_Agent", BRIGHT_MAGENTA),
    ("LLM Fallback", BRIGHT_YELLOW), ("LLM Quota", BRIGHT_YELLOW),
    ("LLM", BRIGHT_GREEN), ("Azure OpenAI", BRIGHT_GREEN), ("Local SLM", BRIGHT_GREEN),
    ("Guardrails", RED),
    ("ProjectStore", DIM + WHITE), ("ProjectsService", DIM + WHITE), ("Supabase", DIM + WHITE),
    ("UserStore", DIM + WHITE), ("History", DIM + WHITE), ("Conversation", DIM + WHITE),
    ("API", DIM), ("Route", DIM),
]
_LEADING_TAG = re.compile(r"^(\s*(?:-->|<--)?\s*)\[([^\]\n]{1,60})\]")
_ALARM = re.compile(r"TRIPWIRE TRIGGERED|Guardrail Block\]")


def _tag_color(tag: str) -> str:
    return next((color for prefix, color in TAG_COLORS if tag.startswith(prefix)), BOLD)


class ConsoleFormatter(logging.Formatter):
    """'HH:MM:SS LEVEL   message' with optional ANSI colours for the time, the level and the leading [Tag]."""

    def __init__(self, use_colors: bool):
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_colors:
            return super().format(record)
        message = record.getMessage()
        if _ALARM.search(message):
            body = BOLD + RED + message + RESET
        else:
            body = _LEADING_TAG.sub(lambda m: f"{m.group(1)}{_tag_color(m.group(2))}[{m.group(2)}]{RESET}", message,
                                    count=1)
        level = LEVEL_COLORS.get(record.levelno, "") + f"{record.levelname:<7}" + RESET
        line = f"{DIM}{self.formatTime(record, self.datefmt)}{RESET} {level} {body}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


class QuietPollingFilter(logging.Filter):
    """Drops uvicorn access lines for endpoints that are polled (progress every 2 s, health checks)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(path in message for path in QUIET_ACCESS_PATHS)


def _enable_windows_ansi() -> bool:
    """Turns on ANSI escape handling for the Windows console (PowerShell, cmd); False if that isn't possible."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)                     # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def _use_colors() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("LOG_COLOR", "").strip().lower() in ("1", "true", "yes", "on"):
        return _enable_windows_ansi() or True
    return bool(getattr(sys.stdout, "isatty", lambda: False)()) and _enable_windows_ansi()


def _handlers() -> List[logging.Handler]:
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter(_use_colors()))
    handlers: List[logging.Handler] = [console]
    if os.getenv("LOG_TO_FILE", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
                                               delay=True)
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                                        datefmt="%Y-%m-%d %H:%M:%S"))
            handlers.append(file_handler)
        except OSError as exc:                                   # e.g. a read-only folder: console only
            print(f"[Logging] File log disabled: {exc}", file=sys.stderr)
    return handlers


_HANDLERS = _handlers()
logging.basicConfig(level=logging.INFO, handlers=_HANDLERS, force=True)

# Uvicorn configures its own loggers before the app is imported; give them the same handlers (time + colours + file)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uvicorn_logger = logging.getLogger(_name)
    _uvicorn_logger.handlers = list(_HANDLERS) if _name != "uvicorn.error" else []
    _uvicorn_logger.propagate = _name == "uvicorn.error"         # uvicorn.error -> uvicorn (one line, not two)
_access = logging.getLogger("uvicorn.access")
if not any(isinstance(f, QuietPollingFilter) for f in _access.filters):
    _access.addFilter(QuietPollingFilter())

logger = logging.getLogger("DevFoundry")
