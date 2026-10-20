"""
Converts structured PRD and TRD JSON objects (stored in Supabase as JSONB)
into human-readable Markdown strings for rendering in the frontend DocumentDrawer.

PRD shape:  dict with keys like Objective, Goals, Functional Requirements, etc.
TRD shape:  dict of dicts — each top-level key is a major section (Backend Stack, Security, etc.)
"""
import re
from typing import Any


# ---------------------------------------------------------------------------
# PRD
# ---------------------------------------------------------------------------

def prd_json_to_markdown(data: Any) -> str:
    """
    Converts a PRD JSONB dict to a Markdown string.

    Section order is preserved to match a natural PRD reading flow.
    """
    if not data or not isinstance(data, dict):
        return ""
    # Preferred display order — any keys not in this list are appended at the end
    ORDERED_KEYS = [
        "Objective",
        "Problem Statement",
        "Target Users",
        "Goals",
        "Non-Goals",
        "User Experience",
        "User Flow",
        "Functional Requirements",
        "Assumptions",
        "Constraints",
        "Open Questions",
    ]

    lines: list[str] = []
    seen: set[str] = set()

    def render_section(key: str, value: Any) -> None:
        lines.append(f"## {key}\n")
        if not value:                       # an empty section is said, not left as a bare heading (ISSUES AGT-42)
            lines.append(f"{EMPTY_TEXT}\n")
            lines.append("")
            return
        if isinstance(value, str):
            lines.append(f"{value}\n")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # Functional Requirements: {"id": "FR-001", "source": "...", "requirement": "..."}
                    req_id = item.get("id", "")
                    req_text = item.get("requirement", "")
                    source = item.get("source", "")
                    if req_id and req_text:
                        lines.append(f"- **{req_id}** _{source}_: {req_text}" if source
                                     else f"- **{req_id}**: {req_text}")
                    else:
                        # Generic dict item
                        lines.append(f"- {', '.join(f'{k}: {v}' for k, v in item.items())}")
                else:
                    lines.append(f"- {item}")
            if key == "Assumptions":
                # numbered, so one can be confirmed or changed by its number ("change assumption 2 to ...", REV-4)
                bullets = [n for n, line in enumerate(lines) if line.startswith("- ")][-len(value):]
                for number, index in enumerate(bullets, start=1):
                    lines[index] = f"{number}. {lines[index][2:]}"
            lines.append("")
        elif isinstance(value, dict):
            for sub_key, sub_val in value.items():
                lines.append(f"### {sub_key}\n")
                if isinstance(sub_val, list):
                    for i in sub_val:
                        lines.append(f"- {i}")
                    lines.append("")
                else:
                    lines.append(f"{sub_val}\n")
        lines.append("")

    for key in ORDERED_KEYS:
        if key in data:
            render_section(key, data[key])
            seen.add(key)

    # Any remaining keys not in the ordered list
    for key, value in data.items():
        if key not in seen:
            render_section(key, value)

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# TRD
# ---------------------------------------------------------------------------

def _clean_mandated(text: Any) -> Any:
    if isinstance(text, str):
        return re.sub(r"\s*\(\s*mandated(?:\s+by\s+user)?\s*\)", "", text, flags=re.IGNORECASE).strip()
    return text


EMPTY_TEXT = "None stated."
# The TRD reads stack first, then infrastructure and security (the model's order varied: Security came first)
TRD_SECTION_ORDER = ("summary", "frontend", "backend", "database", "integration", "infrastructure", "security")


def _trd_section_rank(key: str) -> int:
    lowered = key.lower()
    return next((n for n, word in enumerate(TRD_SECTION_ORDER) if word in lowered), len(TRD_SECTION_ORDER))


def trd_json_to_markdown(data: Any) -> str:
    """
    Converts a TRD JSONB dict to a Markdown string.

    Each top-level key is a major section (Backend Technology Stack, Security, etc.)
    containing nested string/list/dict values. Sections follow TRD_SECTION_ORDER; empty values say "None stated."
    """
    if not data or not isinstance(data, dict):
        return ""
    lines: list[str] = []

    def render_value(key: str, value: Any, depth: int = 0) -> None:
        indent = "  " * depth
        heading = "###" if depth == 0 else "####"

        if value is None or value == "" or value == [] or value == {}:
            lines.append(f"{indent}**{_title(key)}:** {EMPTY_TEXT}\n")
        elif isinstance(value, str):
            clean_str = _clean_mandated(value)
            lines.append(f"{indent}**{_title(key)}:** {clean_str}\n")
        elif isinstance(value, list):
            lines.append(f"{indent}**{_title(key)}:**")
            for item in value:
                clean_item = _clean_mandated(item) if isinstance(item, str) else item
                lines.append(f"{indent}- {clean_item}")
            lines.append("")
        elif isinstance(value, dict):
            # Nested dict gets its own sub-heading
            lines.append(f"{heading} {_title(key)}\n")
            for sub_key, sub_val in value.items():
                render_value(sub_key, sub_val, depth + 1)
        else:
            lines.append(f"{indent}**{_title(key)}:** {_clean_mandated(str(value))}\n")

    for section_key, section_value in sorted(data.items(), key=lambda item: _trd_section_rank(item[0])):
        lines.append(f"## {section_key}\n")
        if isinstance(section_value, dict):
            for k, v in section_value.items():
                render_value(k, v, depth=0)
        elif isinstance(section_value, list):
            for item in section_value:
                lines.append(f"- {item}")
            lines.append("")
        else:
            lines.append(f"{section_value}\n")
        lines.append("")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _title(key: str) -> str:
    """Converts snake_case / camelCase keys to Title Case for display."""
    import re
    # Insert space before uppercase sequences in camelCase
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", key)
    # Replace underscores with spaces
    spaced = spaced.replace("_", " ")
    return spaced.title()
