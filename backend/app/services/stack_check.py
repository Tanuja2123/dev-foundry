"""
Stack check (FEATURES SCAF-R9): compares the TRD's chosen technologies with the generator commands that really ran, so
the reply states the actual stack from data ("Frontend: Next.js ✓ · Backend: Express ✓ · Database: PostgreSQL ✓")
instead of the model naming generators itself (seen: "create-vite for Next.js, uv init for Node.js", ISSUES AGT-27).
No LLM involved.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# technology (as written in a TRD) -> patterns that show it in the generator commands
TECH_SIGNALS: Tuple[Tuple[str, str], ...] = (
    (r"next\.?js", r"create-next-app"),
    (r"nuxt", r"nuxi|create-nuxt"),
    (r"angular", r"@angular/cli"),
    (r"svelte", r"\bsv\b|create-svelte|template\s+svelte"),
    (r"vue", r"create-vue|template\s+vue"),
    (r"react", r"create-next-app|template\s+react|create-vite.*react"),
    (r"astro", r"create-astro"),
    (r"nest\.?js", r"@nestjs/cli"),
    (r"express", r"express-generator|\bexpress\b"),
    (r"fastify", r"fastify"),
    (r"fastapi", r"fastapi"),
    (r"django", r"django"),
    (r"flask", r"\bflask\b"),
    (r"spring", r"spring-boot|start\.spring\.io"),
    (r"quarkus", r"quarkus"),
    (r"micronaut", r"micronaut"),
    (r"\.net|asp\.net|c#", r"\bdotnet\s+new\b"),
    (r"laravel", r"laravel/laravel"),
    (r"symfony", r"symfony"),
    (r"rails", r"\brails\s+new\b"),
    (r"phoenix", r"phx\.new"),
    (r"elixir", r"\bmix\b"),
    (r"flutter", r"flutter\s+create"),
    (r"\bdart\b", r"\bdart\s+create\b|flutter\s+create"),
    (r"\bgo\b|golang", r"\bgo\s+mod\s+init\b"),
    (r"rust", r"\bcargo\b"),
    (r"deno", r"\bdeno\b"),
    (r"\bbun\b", r"\bbun\b"),
    (r"node", r"\bnpm\b|\bnpx\b"),
    (r"python", r"\buv\b|\bpip\b"),
    (r"java\b", r"spring-boot|quarkus|micronaut|\bmvn\b|\bgradle\b"),
    (r"kotlin", r"\bgradle\b|spring-boot"),
    (r"typescript", r"--ts\b|-ts\b|typescript|--typescript"),
)
DB_SIGNALS: Tuple[Tuple[str, str], ...] = (
    (r"postgres", r"postgres|\bpg\b|psycopg|asyncpg|prisma|npgsql|phx\.new(?!.*--database\s+(sqlite3|mysql))"),
    (r"mysql|mariadb", r"mysql|mariadb"),
    (r"sqlite", r"sqlite"),
    (r"mongo", r"mongo"),
)
LAYERS = (("Frontend", ("Frontend Technology Stack",), ("framework", "language")),
          ("Backend", ("Backend Technology Stack",), ("framework", "language")),
          ("Mobile", ("Mobile Technology Stack", "Mobile App Technology Stack"), ("framework", "language")))


def _trd_value(trd: Dict[str, Any], sections: Tuple[str, ...], fields: Tuple[str, ...]) -> Optional[str]:
    for name in sections:
        section = trd.get(name)
        if isinstance(section, dict):
            value = next((str(section.get(f)) for f in fields if section.get(f) and str(section.get(f)).strip()
                          and "not specified" not in str(section.get(f)).lower()), None)
            if value:
                return value.strip()
    return None


def _matches(choice: str, commands_text: str, signals: Tuple[Tuple[str, str], ...]) -> Optional[bool]:
    """True / False when the choice is a known technology, None when we can't tell from the commands."""
    main = re.split(r"\s*[(\[]", choice, maxsplit=1)[0]       # "Next.js (React, TypeScript)" -> "Next.js"
    # the most specific technology named (signals are ordered: Next.js before React, NestJS before Node...)
    command = next((command for tech, command in signals if re.search(tech, main, re.IGNORECASE)), None)
    if command is None:
        return None
    return bool(re.search(command, commands_text, re.IGNORECASE))


def stack_check(trd: Optional[Dict[str, Any]], commands: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """
    One line for the reply, e.g. "**Stack check:** Frontend: Next.js ✓ · Backend: Express ✓ · Database: PostgreSQL ✓".
    ✗ means the TRD's choice was not generated; "not generated" means the generators don't set it up (e.g. a database
    the code only connects to). None when there is no TRD or nothing ran.
    """
    trd = trd or {}
    text = " ".join(f"{c.get('command', '')} {c.get('cwd', '')}" for c in commands or [] if isinstance(c, dict))
    if not trd or not text.strip():
        return None
    items: List[str] = []
    for label, sections, fields in LAYERS:
        choice = _trd_value(trd, sections, fields)
        if not choice:
            continue
        short = choice.split(" (")[0][:40]
        ok = _matches(choice, text, TECH_SIGNALS)
        items.append(f"{label}: {short} " + ("✓" if ok else "✗ (not generated)" if ok is False else "(not checked)"))
    database = _trd_value(trd, ("Database Technology Stack",), ("database",))
    if database:
        ok = _matches(database, text, DB_SIGNALS)
        items.append(f"Database: {database.split(' (')[0][:30]} " + ("✓" if ok else "(set up when you run it)"))
    return ("**Stack check:** " + " · ".join(items)) if items else None
