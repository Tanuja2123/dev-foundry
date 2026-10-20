"""Project README (SCAF-R12): getting started only, written by code from the generated files."""
import json

from app.services.project_readme import build_readme, detect_parts


def f(path, content=""):
    return {"path": path, "content": content}


BOOKNOOK = [
    f("/backend/mix.exs", "defp deps do [{:phoenix, \"~> 1.8\"}, {:ecto_sql, \"~> 3.13\"}, {:postgrex, \">= 0.0.0\"}]"),
    f("/backend/config/dev.exs", "config :booknook, Booknook.Repo, username: \"postgres\""),
    f("/frontend/pubspec.yaml", "name: book_nook\nflutter:\n  uses-material-design: true\n"),
    f("/frontend/lib/main.dart", "void main() {}"),
    f("/frontend/README.md", "# book_nook"),
]
CLINIC = [
    f("/frontend/package.json", json.dumps({"scripts": {"dev": "next dev", "build": "next build"},
                                            "dependencies": {"next": "16.0.10", "react": "19"}})),
    f("/backend/package.json", json.dumps({"scripts": {"start": "node ./bin/www"},
                                           "dependencies": {"express": "~4.16.1"}})),
    f("/backend/bin/www", "#!/usr/bin/env node"),
]


def test_a_two_part_project_gets_simple_setup_steps():
    readme = build_readme("BookNook", "A small bookshop where customers reserve books.", BOOKNOOK)
    assert readme.startswith("# BookNook\n\nA small bookshop where customers reserve books.\n\n## Getting started")
    assert "### Backend (Phoenix) — `backend/`" in readme and "### Mobile app (Flutter) — `frontend/`" in readme
    assert "```bash\ncd backend\nmix setup\n```" in readme and "```bash\ncd backend\nmix phx.server\n```" in readme
    assert "```bash\ncd frontend\nflutter pub get\n```" in readme and "PostgreSQL running locally" in readme
    assert "## Environment variables" in readme and "| `SECRET_KEY_BASE` | backend |" in readme
    assert "```bash\ncd backend\nmix test\n```" in readme
    # scope (decided): no feature list or tech-stack section
    assert "Features" not in readme and "Tech stack" not in readme and "Technology" not in readme


def test_scripts_come_from_package_json_and_no_secret_values_are_copied():
    files = CLINIC + [f("/backend/.env.example", "DATABASE_URL=postgres://user:hunter2@db/app\nJWT_SECRET=change-me\n")]
    readme = build_readme("Clinic", None, files)
    assert "```bash\ncd frontend\nnpm install\n```" in readme and "```bash\ncd frontend\nnpm run dev\n```" in readme
    # Express moves to 4000 next to Next.js on 3000 (AGT-47: both said 3000)
    assert "```bash\ncd backend\nPORT=4000 npm start\n```" in readme and "http://localhost:3000" in readme
    assert "| `DATABASE_URL` | backend |" in readme and "| `JWT_SECRET` | backend |" in readme
    assert "hunter2" not in readme and "change-me" not in readme


def test_a_single_root_project_and_unknown_layouts():
    fastapi = [f("/pyproject.toml", "[project]\nname='api'\ndependencies=['fastapi[standard]', 'pytest']"),
               f("/src/api/main.py", "app = FastAPI()")]
    parts = detect_parts(fastapi)
    assert len(parts) == 1 and parts[0].title == "API (FastAPI)" and parts[0].run == ["uv run fastapi dev src/api/main.py"]
    readme = build_readme("API", None, fastapi)
    assert "```bash\nuv sync\n```" in readme and "cd " not in readme and "uv run pytest" in readme
    assert build_readme("Nothing", None, [f("/notes.txt", "x")]) is None             # nothing reliable to say


def test_full_stack_readme_has_separate_ports_an_api_and_the_database_variable():
    # seen (GymPass): "a api", both parts on port 3000, and nothing about the installed database library (AGT-47)
    import json as _json
    structure = [
        {"path": "/backend/package.json", "content": _json.dumps({"scripts": {"start": "node ./bin/www"},
                                                                  "dependencies": {"express": "~4.16.1", "prisma": "^6"}})},
        {"path": "/frontend/package.json", "content": _json.dumps({"scripts": {"dev": "next dev"},
                                                                   "dependencies": {"next": "16"}})}]
    text = build_readme("GymPass", "Gym app.", structure)
    assert "The project has an API in `backend/` and a web app in `frontend/`." in text
    assert "cd backend\nPORT=4000 npm start" in text and "(PowerShell: `$env:PORT=4000; npm start`)" in text
    assert "cd frontend\nnpm run dev" in text and "Then open http://localhost:3000." in text
    assert "| `DATABASE_URL` | backend |" in text
    assert "Set these as environment variables before you start the part" in text


def test_a_single_api_keeps_its_default_port():
    import json as _json
    text = build_readme("Api", None, [{"path": "/package.json", "content": _json.dumps(
        {"scripts": {"start": "node ./bin/www"}, "dependencies": {"express": "4"}})}])
    assert "npm start\n```" in text and "PORT=4000" not in text and "http://localhost:3000" in text
