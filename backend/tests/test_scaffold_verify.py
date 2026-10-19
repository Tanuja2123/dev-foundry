"""'Verify the scaffold' (SCAF-R12, on request): install + build per part in a temporary copy, ✓ / ✗ per part."""
import json
import subprocess
from types import SimpleNamespace

from app.services import scaffold_verify as sv
from app.services import workflow_routing as wr

STRUCTURE = [
    {"path": "/frontend/package.json", "content": json.dumps({"scripts": {"dev": "next dev", "build": "next build"},
                                                              "dependencies": {"next": "16"}})},
    {"path": "/backend/mix.exs", "content": "defp deps, do: [{:phoenix, \"~> 1.8\"}]"},
    {"path": "/backend/priv/static/favicon.ico", "content": "AAAA", "encoding": "base64"},
]


def fake_runner(fail_on=None, calls=None):
    def run(args, cwd, **kwargs):
        calls.append((" ".join([args[0].rsplit("\\\\", 1)[-1].rsplit("/", 1)[-1].replace(".cmd", "")] + args[1:]), cwd))
        failed = fail_on and fail_on in " ".join(args)
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="** (CompileError) lib/x.ex:3: undefined" if failed else "")
    return run


def test_each_part_is_installed_and_built(monkeypatch):
    monkeypatch.setattr(sv.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    results = sv.verify_structure(STRUCTURE, runner=fake_runner(calls=calls))
    assert [(r["part"], r["status"]) for r in results] == [("Backend (Phoenix)", "ok"), ("Web app (Next.js)", "ok")]
    commands = [c for c, _ in calls]
    assert commands == ["mix deps.get", "mix compile", "npm install --ignore-scripts --no-audit --no-fund",
                        "npm run build"]
    assert __import__("os").path.basename(calls[0][1]) == "backend"       # each part in its own folder
    text = sv.render_verification(results)
    assert "Backend (Phoenix) — `backend/` ✓" in text and text.endswith("Say **push to main** when you're ready.")


def test_a_failure_shows_the_error_and_a_missing_tool_is_not_guessed(monkeypatch):
    monkeypatch.setattr(sv.shutil, "which", lambda name: None if name == "npm" else f"/usr/bin/{name}")
    results = sv.verify_structure(STRUCTURE, runner=fake_runner(fail_on="compile", calls=[]))
    by_part = {r["part"]: r for r in results}
    assert by_part["Backend (Phoenix)"]["status"] == "failed" and "`mix compile` failed" in by_part["Backend (Phoenix)"]["detail"]
    assert by_part["Web app (Next.js)"]["status"] == "skipped" and "`npm` isn't installed" in by_part["Web app (Next.js)"]["detail"]
    text = sv.render_verification(results)
    assert "✗ `mix compile` failed:" in text and "```text\n** (CompileError)" in text
    assert "not checked, `npm` isn't installed on the DevFoundry server" in text
    assert text.endswith("Tell me what to fix, or ask me to regenerate the structure.")


def test_a_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(sv.shutil, "which", lambda name: f"/usr/bin/{name}")

    def slow(args, cwd, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)
    results = sv.verify_structure(STRUCTURE[1:2], runner=slow)
    assert results[0]["status"] == "failed" and "took longer than" in results[0]["detail"]


def test_verification_is_offered_once_a_structure_exists():
    state = {"project": {"prd": {"x": 1}, "trd": {"x": 1}, "workflow_stage": "STRUCTURE_REVIEW", "ado_project_id": "p"},
             "repository": {"structure": STRUCTURE, "ado_repo_id": "r", "status": "STRUCTURE_REVIEW",
                            "pushed_structure": None}}
    assert "VERIFY_REPOSITORY_STRUCTURE" in wr.allowed_operations("STRUCTURE_REVIEW", state)
    assert wr.OPERATION_AGENT["VERIFY_REPOSITORY_STRUCTURE"] == "Repo_Setup_Agent"
    labels = [s["label"] for s in wr.next_steps(state)]
    assert "Verify the scaffold installs and builds" in labels
    no_structure = {"project": state["project"], "repository": {"ado_repo_id": "r"}}
    assert "VERIFY_REPOSITORY_STRUCTURE" not in wr.allowed_operations("STRUCTURE_REVIEW", no_structure)
