"""
Project README (FEATURES SCAF-R12): how to set the generated project up and run it. Written by code from the stored
structure (never by the model), so every command matches the files that are actually in the repository.

Scope (decided 2026-10-16): only getting started - prerequisites, setup, run, tests and environment variables (names and
purpose, never values) - in plain words; no feature list or tech-stack section. Each part of the repository (a folder
with its own manifest: package.json, mix.exs, pubspec.yaml, pyproject.toml, ...) gets its own short section.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

README_PATH = "/README.md"


@dataclass
class Part:
    folder: str                                   # "" for the repository root, else "frontend", "backend", ...
    title: str                                    # "Web app (Next.js)", "Backend (Phoenix)", ...
    prerequisites: List[str]
    setup: List[str]
    run: List[str]
    run_note: str = ""
    test: List[str] = field(default_factory=list)
    env: List[Tuple[str, str]] = field(default_factory=list)   # (NAME, purpose)


def _files_by_path(files: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {f["path"]: f for f in files or [] if isinstance(f, dict) and f.get("path")}


def _text(files: Dict[str, Dict[str, Any]], path: str) -> str:
    item = files.get(path) or {}
    return "" if item.get("encoding") == "base64" else str(item.get("content") or "")


def _env_example(files: Dict[str, Dict[str, Any]], folder: str) -> List[Tuple[str, str]]:
    """Variable names from a generated .env.example (values are never copied)."""
    text = _text(files, f"{folder}/.env.example" if folder else "/.env.example")
    names = []
    for line in text.splitlines():
        match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match.group(1) not in [n for n, _ in names]:
            names.append((match.group(1), "see `.env.example`"))
    return names[:20]


def _npm_part(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    try:
        package = json.loads(_text(files, f"{prefix}/package.json") or "{}")
    except json.JSONDecodeError:
        package = {}
    scripts = package.get("scripts") or {}
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    if "next" in deps:
        title, port = "Web app (Next.js)", 3000
    elif "@nestjs/core" in deps:
        title, port = "API (NestJS)", 3000
    elif "express" in deps:
        title, port = "API (Express)", 3000
    elif "vite" in deps:
        title, port = "Web app (Vite)", 5173
    elif "@angular/core" in deps:
        title, port = "Web app (Angular)", 4200
    else:
        title, port = "App (Node.js)", None
    script = next((s for s in ("dev", "start:dev", "start") if s in scripts), None)
    run = [f"npm run {script}" if script not in ("start",) else "npm start"] if script else []
    env = _env_example(files, prefix)
    if "express" in deps and not env:
        env = [("PORT", f"port the API listens on (default {port})")]
    return Part(folder=folder, title=title, prerequisites=["Node.js (current LTS) and npm"], setup=["npm install"],
                run=run, run_note=f"Then open http://localhost:{port}" if port and run else "",
                test=["npm test"] if "test" in scripts and "no test specified" not in str(scripts.get("test")) else [],
                env=env)


def _mix_part(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    mix = _text(files, f"{prefix}/mix.exs")
    if ":phoenix" in mix or "phoenix" in mix.lower():
        uses_ecto = "ecto" in mix.lower()
        return Part(folder=folder, title="Backend (Phoenix)",
                    prerequisites=["Elixir and Erlang/OTP"] + (["PostgreSQL running locally (user and password "
                                                                "`postgres`, as in `config/dev.exs`)"] if uses_ecto else []),
                    setup=["mix setup"], run=["mix phx.server"], run_note="Then open http://localhost:4000",
                    test=["mix test"],
                    env=_env_example(files, prefix) or [
                        ("DATABASE_URL", "database connection in production"),
                        ("SECRET_KEY_BASE", "secret for signing cookies in production (`mix phx.gen.secret`)"),
                        ("PHX_HOST", "public host name in production")])
    return Part(folder=folder, title="App (Elixir)", prerequisites=["Elixir and Erlang/OTP"], setup=["mix deps.get"],
                run=["mix run --no-halt"], test=["mix test"], env=_env_example(files, prefix))


def _flutter_part(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    pubspec = _text(files, f"{prefix}/pubspec.yaml")
    if "flutter:" in pubspec:
        return Part(folder=folder, title="Mobile app (Flutter)",
                    prerequisites=["Flutter SDK", "Android Studio (Android emulator) or Xcode (iOS simulator, macOS only)"],
                    setup=["flutter pub get"], run=["flutter run"],
                    run_note="Start an emulator or connect a device first", test=["flutter test"],
                    env=_env_example(files, prefix))
    return Part(folder=folder, title="App (Dart)", prerequisites=["Dart SDK"], setup=["dart pub get"],
                run=["dart run"], test=["dart test"], env=_env_example(files, prefix))


def _python_part(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    pyproject = _text(files, f"{prefix}/pyproject.toml").lower()
    mains = sorted(p[len(prefix) + 1:] for p in files if p.startswith(f"{prefix}/") and p.endswith("/main.py"))
    run, title = [], "App (Python)"
    if "fastapi" in pyproject:
        title = "API (FastAPI)"
        run = [f"uv run fastapi dev {mains[0]}"] if mains else []
    elif "django" in pyproject:
        title, run = "Web app (Django)", ["uv run python manage.py runserver"] if f"{prefix}/manage.py" in files else []
    elif "flask" in pyproject:
        title = "Web app (Flask)"
    scripts = re.search(r"\[project\.scripts\]\s*\n\s*([a-z0-9_-]+)\s*=", pyproject)
    if not run and scripts:
        run = [f"uv run {scripts.group(1)}"]
    return Part(folder=folder, title=title, prerequisites=["Python 3", "uv (https://docs.astral.sh/uv/)"],
                setup=["uv sync"], run=run,
                run_note="" if run else "Add your entry point, then start it with `uv run python <file>`",
                test=["uv run pytest"] if "pytest" in pyproject else [], env=_env_example(files, prefix))


def _simple(title: str, prerequisites: List[str], setup: List[str], run: List[str], test: List[str]):
    def build(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
        return Part(folder=folder, title=title, prerequisites=prerequisites, setup=setup, run=run, test=test,
                    env=_env_example(files, prefix))
    return build


def _jvm(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    maven = f"{prefix}/pom.xml" in files
    wrapper = "./mvnw" if f"{prefix}/mvnw" in files else ("./gradlew" if f"{prefix}/gradlew" in files else None)
    tool = wrapper or ("mvn" if maven else "gradle")
    text = (_text(files, f"{prefix}/pom.xml") + _text(files, f"{prefix}/build.gradle")
            + _text(files, f"{prefix}/build.gradle.kts")).lower()
    spring = "spring-boot" in text or "org.springframework.boot" in text
    run = [f"{tool} spring-boot:run" if maven else f"{tool} bootRun"] if spring else (
        [f"{tool} run"] if not maven else [])
    return Part(folder=folder, title="Backend (Spring Boot)" if spring else "App (Java/Kotlin)",
                prerequisites=["Java (JDK 21 or the version in the build file)"],
                setup=[f"{tool} {'compile' if maven else 'build'}"], run=run,
                run_note="Then open http://localhost:8080" if spring else "", test=[f"{tool} test"],
                env=_env_example(files, prefix))


def _laravel(folder: str, files: Dict[str, Dict[str, Any]], prefix: str) -> Part:
    laravel = f"{prefix}/artisan" in files
    if laravel:
        return Part(folder=folder, title="Web app (Laravel)", prerequisites=["PHP 8.2 or later", "Composer"],
                    setup=["composer install", "cp .env.example .env", "php artisan key:generate"],
                    run=["php artisan serve"], run_note="Then open http://localhost:8000", test=["php artisan test"],
                    env=_env_example(files, prefix))
    return Part(folder=folder, title="App (PHP)", prerequisites=["PHP", "Composer"], setup=["composer install"],
                run=[], test=[], env=_env_example(files, prefix))


# manifest file name -> builder (first match per folder wins)
MANIFESTS = (
    ("package.json", _npm_part),
    ("mix.exs", _mix_part),
    ("pubspec.yaml", _flutter_part),
    ("pyproject.toml", _python_part),
    ("go.mod", _simple("App (Go)", ["Go"], ["go mod download"], ["go run ."], ["go test ./..."])),
    ("Cargo.toml", _simple("App (Rust)", ["Rust (rustup)"], ["cargo build"], ["cargo run"], ["cargo test"])),
    ("pom.xml", _jvm), ("build.gradle", _jvm), ("build.gradle.kts", _jvm),
    ("composer.json", _laravel),
    ("Gemfile", _simple("Web app (Ruby on Rails)", ["Ruby", "Bundler"], ["bundle install", "bin/rails db:setup"],
                        ["bin/rails server"], ["bin/rails test"])),
    ("deno.json", _simple("App (Deno)", ["Deno"], [], ["deno task dev"], ["deno test"])),
)


def detect_parts(structure: List[Dict[str, Any]]) -> List[Part]:
    """The project's parts: the repository root and each top-level folder with a known manifest file."""
    files = _files_by_path(structure)
    folders = [""] + sorted({p.strip("/").split("/")[0] for p in files if p.strip("/").count("/") >= 1})
    parts = []
    for folder in folders:
        prefix = f"/{folder}" if folder else ""
        for manifest, build in MANIFESTS:
            if f"{prefix}/{manifest}" in files:
                parts.append(build(folder, files, prefix))
                break
        if not folder and parts:            # a single-part project at the root: no sub-parts to look for
            break
    for name in ("csproj", "fsproj"):
        if not parts and any(p.endswith(f".{name}") for p in files):
            parts.append(Part(folder="", title="App (.NET)", prerequisites=[".NET SDK"], setup=["dotnet restore"],
                              run=["dotnet run"], test=["dotnet test"]))
    return parts


def _commands(lines: List[str], folder: str) -> str:
    steps = ([f"cd {folder}"] if folder else []) + lines
    return "```bash\n" + "\n".join(steps) + "\n```"


def build_readme(project_name: str, description: Optional[str], structure: List[Dict[str, Any]]) -> Optional[str]:
    """The README text, or None when no part of the project could be recognised (nothing reliable to say)."""
    parts = detect_parts(structure)
    if not parts:
        return None
    lines = [f"# {project_name}", ""]
    if description:
        lines += [description.strip().split("\n")[0], ""]
    lines += ["## Getting started", ""]
    if len(parts) > 1:
        lines += ["The project has " + " and ".join(f"a {p.title.split(' (')[0].lower()} in `{p.folder}/`" for p in parts)
                  + ". Set up each part in its own terminal.", ""]
    for part in parts:
        heading = f"### {part.title}" + (f" — `{part.folder}/`" if part.folder else "")
        lines += [heading, "", "You need: " + "; ".join(part.prerequisites) + ".", ""]
        if part.setup:
            lines += ["Set it up once:", "", _commands(part.setup, part.folder), ""]
        if part.run:
            lines += ["Start it:", "", _commands(part.run, part.folder), ""]
        if part.run_note:
            lines += [part.run_note + ".", ""]
    env = [(p, name, purpose) for p in parts for name, purpose in p.env]
    lines += ["## Environment variables", ""]
    if env:
        lines += ["Set these in your environment (or a local `.env` file that is never committed):", "",
                  "| Variable | Part | What it is for |", "|---|---|---|"]
        lines += [f"| `{name}` | {p.folder or 'root'} | {purpose} |" for p, name, purpose in env]
    else:
        lines += ["No environment variables are needed to run the project locally. Keep any secrets you add later "
                  "out of the repository."]
    lines.append("")
    tests = [p for p in parts if p.test]
    if tests:
        lines += ["## Running the tests", ""]
        for part in tests:
            lines += [f"{part.title}:", "", _commands(part.test, part.folder), ""]
    return "\n".join(lines).rstrip() + "\n"
