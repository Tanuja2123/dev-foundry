"""
"Verify the scaffold" (FEATURES SCAF-R12, decided D3 = on request): installs the dependencies and builds each part of
the saved structure in a temporary copy on the DevFoundry server, and reports ✓ / ✗ per part. Nothing in the project
or the repository changes. No LLM involved.

Safety: dependencies are installed with the package managers' "no install scripts" options where they exist, inside a
throwaway folder that is deleted afterwards; a part whose toolchain is not installed on the server is reported as "not
checked" instead of being guessed.
"""
import base64
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.services import progress
from app.services.project_readme import detect_parts
from app.services.scaffold_service import _command_env

DEFAULT_TIMEOUT_SECONDS = 600


def verify_timeout_seconds() -> int:
    try:
        return max(60, int(os.getenv("SCAFFOLD_VERIFY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def verify_commands(title: str, files: Dict[str, Dict[str, Any]], prefix: str) -> List[str]:
    """The install + build steps for one part (the same tools the README tells developers to use)."""
    if "(Next.js)" in title or "(Vite)" in title or "(Angular)" in title or "(NestJS)" in title \
            or "(Express)" in title or "(Node.js)" in title:
        package = str((files.get(f"{prefix}/package.json") or {}).get("content") or "")
        steps = ["npm install --ignore-scripts --no-audit --no-fund"]
        if '"build"' in package:
            steps.append("npm run build")
        return steps
    if "(Phoenix)" in title or "(Elixir)" in title:
        return ["mix deps.get", "mix compile"]
    if "(Flutter)" in title:
        return ["flutter pub get", "flutter analyze --no-fatal-infos --no-fatal-warnings"]
    if "(Dart)" in title:
        return ["dart pub get", "dart analyze --no-fatal-warnings"]
    if "(FastAPI)" in title or "(Django)" in title or "(Flask)" in title or "(Python)" in title:
        return ["uv sync", "uv run python -m compileall -q ."]
    if "(Go)" in title:
        return ["go build ./..."]
    if "(Rust)" in title:
        return ["cargo build"]
    if "(Spring Boot)" in title or "(Java/Kotlin)" in title:
        if f"{prefix}/pom.xml" in files:
            return ["mvn -q -B compile"]
        return ["gradle build -x test --no-daemon"]
    if "(Laravel)" in title or "(PHP)" in title:
        return ["composer install --no-scripts --no-interaction"]
    if "(Ruby on Rails)" in title:
        return ["bundle install"]
    if "(.NET)" in title:
        return ["dotnet build"]
    if "(Deno)" in title:
        return ["deno check ."]
    return []


def _materialize(structure: List[Dict[str, Any]], root: str) -> None:
    for item in structure:
        path = os.path.join(root, *item["path"].strip("/").split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = base64.b64decode(item.get("content") or "") if item.get("encoding") == "base64" else \
            str(item.get("content") or "").encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(data)


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join([line for line in (text or "").strip().splitlines() if line.strip()][-lines:])[:1500]


def verify_structure(structure: List[Dict[str, Any]], runner=subprocess.run) -> List[Dict[str, Any]]:
    """[{"part", "folder", "status": ok|failed|skipped, "commands", "detail"}] for every recognised part."""
    files = {f["path"]: f for f in structure or [] if isinstance(f, dict) and f.get("path")}
    parts = detect_parts(structure)
    results: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="devfoundry-verify-") as root:
        _materialize(structure, root)
        for part in parts:
            prefix = f"/{part.folder}" if part.folder else ""
            commands = verify_commands(part.title, files, prefix)
            result = {"part": part.title, "folder": part.folder, "status": "ok", "commands": commands, "detail": ""}
            if not commands:
                result.update(status="skipped", detail="no build step is known for this part")
            missing = sorted({c.split()[0] for c in commands if not shutil.which(c.split()[0])})
            if missing:
                result.update(status="skipped",
                              detail=f"`{'`, `'.join(missing)}` isn't installed on the DevFoundry server")
                commands = []
            cwd = os.path.join(root, *part.folder.split("/")) if part.folder else root
            for command in commands:
                progress.detail(f"Running `{command}`" + (f" in {part.folder}/" if part.folder else ""))
                started = time.time()
                args = shlex.split(command)
                try:
                    # Same way the generators run: the resolved executable (npm.cmd on Windows), no shell, no stdin
                    done = runner([shutil.which(args[0]) or args[0], *args[1:]], cwd=cwd, env=_command_env(),
                                  shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=verify_timeout_seconds())
                except subprocess.TimeoutExpired:
                    result.update(status="failed", detail=f"`{command}` took longer than {verify_timeout_seconds()} s")
                    break
                except OSError as exc:
                    result.update(status="failed", detail=f"`{command}` could not start: {exc}")
                    break
                logger.info(f"[Scaffold Verify] {part.title}: `{command}` -> {done.returncode} "
                            f"({time.time() - started:.0f}s)")
                if done.returncode != 0:
                    result.update(status="failed", detail=f"`{command}` failed:\n" + _tail(done.stderr or done.stdout))
                    break
            results.append(result)
    return results


def render_verification(results: List[Dict[str, Any]]) -> str:
    if not results:
        return ("I couldn't recognise any part of this structure to check (no known project files), so nothing was "
                "verified.\n\n---\n\nTell me which stack it uses, or regenerate the structure.")
    lines = ["**Scaffold check**: I installed the dependencies and built each part in a temporary copy (nothing in "
             "your project changed).", ""]
    for r in results:
        where = f" — `{r['folder']}/`" if r["folder"] else ""
        if r["status"] == "ok":
            lines.append(f"- {r['part']}{where} ✓ (" + ", ".join(f"`{c}`" for c in r["commands"]) + ")")
        elif r["status"] == "skipped":
            lines.append(f"- {r['part']}{where}: not checked, {r['detail']}")
        else:
            first, _, rest = r["detail"].partition("\n")
            lines.append(f"- {r['part']}{where} ✗ {first}")
            if rest.strip():
                lines += ["", "```text", rest.strip(), "```"]
    failed = [r for r in results if r["status"] == "failed"]
    lines += ["", "---", ""]
    lines.append("Tell me what to fix, or ask me to regenerate the structure." if failed else
                 "Everything that could be checked installs and builds. Say **push to main** when you're ready.")
    return "\n".join(lines)
