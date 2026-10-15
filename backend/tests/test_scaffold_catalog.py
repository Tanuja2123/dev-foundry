"""
Scaffold catalog + online initializers:
- the catalog is consistent with the allow-lists, adds headless flags to the generator (not to npx/uvx), validates guarded
  executables, detects missing expected files, and describes only installed generators (plus what is NOT available);
- online initializers (Spring Boot, Quarkus, Micronaut) build validated requests, download from allow-listed hosts only,
  extract safely (single top-level folder stripped, zip-slip rejected) and report service errors with suggestions;
- the Repo Setup Agent's scaffold tool accepts initializers and records them (with the reproducible request URL).
Live calls to the real services are opt-in: SCAFFOLD_LIVE_TESTS=1 pytest tests/test_scaffold_catalog.py -m integration
"""
import io
import os
import subprocess
import zipfile

import httpx
import pytest

from app.services import project_store as store
from app.services import scaffold_catalog as catalog
from app.services import scaffold_initializers as init
from app.services import scaffold_service as ss
from app.services.agents import repo_setup_agent as rsa
from app.services.conversation_service import render_structure_for_display
from app.services.mcp.ado_git_push import AdoPushError
from tests.test_scaffold_service import ado, call, created_repo, db  # noqa: F401  (fixtures / helpers)

pytestmark = pytest.mark.usefixtures("no_real_network")

CONV = "conv-scaffold-test"


# ---------------------------------------------------------------------------
# Catalog consistency
# ---------------------------------------------------------------------------

def test_every_generator_is_allowed_by_the_command_rules():
    ids = [g.id for g in catalog.GENERATORS]
    assert len(ids) == len(set(ids))
    for g in catalog.GENERATORS:
        assert g.executable in catalog.ALLOWED_COMMANDS, g.id
        if g.executable in ("npx", "uvx"):
            assert set(g.match) <= catalog.DEFAULT_ALLOWED_PACKAGES, g.id
        else:
            assert set(g.match) <= set(catalog.ALLOWED_COMMANDS[g.executable]), g.id


@pytest.mark.parametrize("command, generator, expected_args", [
    # create-next-app gets its OWN --yes (npx --yes is a different flag) plus skip-install / no git
    ("npx --yes create-next-app@latest web --ts", "next",
     "npx --yes create-next-app@latest web --ts --yes --skip-install --disable-git"),
    ("npx --yes @angular/cli@latest new shop --directory web --skip-install", "angular",
     "npx --yes @angular/cli@latest new shop --directory web --skip-install --defaults --skip-git"),
    ("flutter create --org com.example app", "flutter", "flutter create --org com.example app --no-pub"),
    ("composer create-project laravel/laravel api", "composer",
     "composer create-project laravel/laravel api --no-install --no-scripts --no-interaction"),
    ("rails new web --database=postgresql", "rails", "rails new web --database=postgresql --skip-bundle --skip-git"),
    ("mvn archetype:generate -DgroupId=com.example -DartifactId=app", "maven",
     "mvn archetype:generate -DgroupId=com.example -DartifactId=app -B -DinteractiveMode=false"),
    ("uv add fastapi[standard]", "uv-add", "uv add fastapi[standard] --no-sync"),
    ("npx --yes create-vite@latest frontend --template react-ts", "vite",
     "npx --yes create-vite@latest frontend --template react-ts"),
    ("dotnet new webapi -o Api --no-restore", "dotnet", "dotnet new webapi -o Api --no-restore"),
    ("bun create vite web", "vite", "bun create vite web"),
    ("mix new envcheck --sup", "mix", "mix new envcheck --sup"),
    ("mix phx.new shop --database sqlite3", "phoenix", "mix phx.new shop --database sqlite3 --no-install"),
    ("gradle init --type kotlin-application", "gradle", "gradle init --type kotlin-application --use-defaults --no-daemon"),
])
def test_generators_are_recognised_and_made_headless(command, generator, expected_args):
    parsed, args = ss.parse_command({"command": command})
    assert parsed.generator == generator
    assert " ".join(args) == expected_args


@pytest.mark.parametrize("command, fragment", [
    ("composer create-project evil/package x", "composer create-project may only use"),
    ("swift package resolve", "Only `swift package init`"),
    ("flutter run", "not allowed"),
    ("rails server", "not allowed"),
    ("mvn deploy", "not allowed"),
])
def test_new_executables_are_restricted(command, fragment):
    with pytest.raises(AdoPushError) as info:
        ss.parse_command({"command": command})
    assert fragment in info.value.message


def _write(root, rel, text="x"):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_missing_expected_files(tmp_path):
    vite = next(g for g in catalog.GENERATORS if g.id == "vite")
    _write(str(tmp_path), "web/package.json")
    _write(str(tmp_path), "web/index.html")
    assert catalog.missing_expected_files(str(tmp_path), vite) == ["vite.config.*"]
    _write(str(tmp_path), "web/vite.config.ts")
    assert catalog.missing_expected_files(str(tmp_path), vite) == []
    dotnet = next(g for g in catalog.GENERATORS if g.id == "dotnet")
    assert catalog.missing_expected_files(str(tmp_path), dotnet)          # nothing .NET yet
    _write(str(tmp_path), "Api/Api.csproj")
    assert catalog.missing_expected_files(str(tmp_path), dotnet) == []     # any one project file is enough


def test_catalog_description_lists_installed_generators_initializers_and_missing_toolchains():
    text = catalog.describe_scaffold_catalog({"npx", "uv", "uvx", "dotnet", "npm"}, init.describe_initializers())
    assert "create-vite@latest" in text and "uv init" in text and "dotnet new webapi" in text
    assert "spring-boot" in text and "quarkus" in text and "micronaut" in text
    not_available = text.split("NOT available on this server")[1]
    assert "flutter" in not_available and "rails" in not_available and "composer" in not_available
    assert "flutter create" not in text                                     # unavailable generators are not offered


def test_expected_file_warnings_are_returned(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ss, "_command_version", lambda command, args: None)

    def fake_run(argv, **kwargs):
        _write(kwargs["cwd"], "web/README.md", "# only a readme")       # no package.json / vite.config / index.html
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss.run_scaffold_commands([{"command": "npx --yes create-vite@latest web --template react-ts"}])
    assert result.warnings and "package.json" in result.warnings[0]


def test_command_versions_are_recorded(monkeypatch):
    monkeypatch.setattr(ss, "_registry_version", lambda runner, spec: f"{ss._package_name(spec)} 7.1.2")
    cmd, args = ss.parse_command({"command": "npx --yes create-vite@latest web --template react-ts"})
    assert ss._command_version(cmd, args) == "create-vite 7.1.2"
    assert ss._registry_version.__name__ == "<lambda>"
    # pinned versions are taken from the command itself (no lookup)
    monkeypatch.undo()
    assert ss._registry_version("npx", "create-vite@7.0.0") == "create-vite 7.0.0"


# ---------------------------------------------------------------------------
# Online initializers (mocked HTTP)
# ---------------------------------------------------------------------------

def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


SPRING_ZIP = make_zip({
    "demo/pom.xml": "<project/>",
    "demo/mvnw": "#!/bin/sh\n",
    "demo/.mvn/wrapper/maven-wrapper.properties": "distributionUrl=x\n",
    "demo/src/main/java/com/example/demo/DemoApplication.java": "class DemoApplication {}\n",
    "demo/src/main/resources/application.properties": "",
})


@pytest.fixture
def services(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "start.spring.io" and path == "/starter.zip":
            if "nope" in str(request.url):
                return httpx.Response(400, json={"message": "Unknown dependency 'nope' check project metadata"})
            return httpx.Response(200, content=SPRING_ZIP, headers={"content-type": "application/zip"})
        if host == "start.spring.io" and path == "/metadata/client":
            return httpx.Response(200, json={"bootVersion": {"default": "4.1.1"},
                                             "dependencies": {"values": [{"values": [{"id": "web"}, {"id": "data-jpa"}]}]}})
        if host == "code.quarkus.io":
            return httpx.Response(503, text="down")
        if host == "launch.micronaut.io":
            return httpx.Response(201, content=make_zip({"api/build.gradle": "plugins {}", "api/src/Application.java": "x"}))
        return httpx.Response(404)

    monkeypatch.setattr(init, "transport_factory", lambda: httpx.MockTransport(handler))
    return seen


def test_spring_initializer_request_and_extraction(services, tmp_path):
    run = init.run_initializer({"name": "spring-boot", "folder": "backend",
                                "options": {"build": "gradle-kotlin", "language": "kotlin", "java_version": "21",
                                            "dependencies": "web,data-jpa", "group": "com.acme", "artifact": "shop"}},
                               str(tmp_path), "repo")
    query = dict(services[0].url.params)
    assert query["type"] == "gradle-project-kotlin" and query["language"] == "kotlin" and query["javaVersion"] == "21"
    assert query["dependencies"] == "web,data-jpa" and query["packageName"] == "com.acme.shop"
    assert "bootVersion" not in query                                    # latest stable unless asked otherwise
    # the zip's top-level folder is dropped: files land exactly in backend/
    assert (tmp_path / "backend" / "pom.xml").exists()
    assert (tmp_path / "backend" / "src/main/java/com/example/demo/DemoApplication.java").exists()
    assert run.to_dict() == {"command": f"GET {run.url}", "cwd": "backend", "initializer": "spring-boot",
                             "version": "Spring Boot 4.1.1"}
    assert run.url.startswith("https://start.spring.io/starter.zip?type=gradle-project-kotlin")


def test_micronaut_initializer(services, tmp_path):
    run = init.run_initializer({"name": "micronaut", "options": {"artifact": "Api", "features": "data-jdbc,postgres"}},
                               str(tmp_path), "repo")
    assert services[0].url.path == "/create/default/com.example.api"
    assert dict(services[0].url.params)["build"] == "GRADLE"
    assert (tmp_path / "build.gradle").exists() and run.folder == "."


def test_service_errors(services, tmp_path):
    with pytest.raises(AdoPushError) as bad:
        init.run_initializer({"name": "spring-boot", "options": {"dependencies": "nope"}}, str(tmp_path), "repo")
    assert bad.value.error_type == "VALIDATION_ERROR" and "Unknown dependency 'nope'" in bad.value.message
    with pytest.raises(AdoPushError) as down:
        init.run_initializer({"name": "quarkus"}, str(tmp_path), "repo")
    assert down.value.error_type == "INITIALIZER_UNAVAILABLE"


@pytest.mark.parametrize("spec, fragment", [
    ({"name": "jhipster"}, "Unknown initializer"),
    ({"name": "spring-boot", "folder": "../outside"}, "relative folder"),
    ({"name": "spring-boot", "options": {"build": "ant"}}, "must be one of"),
    ({"name": "spring-boot", "options": {"group": "com.example;rm"}}, "invalid value"),
])
def test_initializer_validation_happens_before_any_request(services, tmp_path, spec, fragment):
    with pytest.raises(AdoPushError) as info:
        init.run_initializer(spec, str(tmp_path), "repo")
    assert fragment in info.value.message and services == []


def test_zip_slip_and_bad_zip_are_rejected(tmp_path):
    with pytest.raises(AdoPushError, match="Unsafe path"):
        init.extract_zip(make_zip({"demo/../../evil.txt": "x", "demo/ok.txt": "y"}), str(tmp_path))
    with pytest.raises(AdoPushError, match="valid zip"):
        init.extract_zip(b"not a zip", str(tmp_path))
    assert not (tmp_path.parent / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Agent tool + display
# ---------------------------------------------------------------------------

def test_scaffold_tool_with_initializer_and_display(db, ado, services, monkeypatch):
    monkeypatch.setattr(ss, "_command_version", lambda command, args: None)
    tools = created_repo(db, ado, repo_name="shop-api")
    result = call(tools, "scaffold_repository_structure",
                  initializers=[rsa.InitializerInput(name="spring-boot", folder=".", options={"dependencies": "web"})])
    assert result["status"] == "success" and "/pom.xml" in result["paths"] and "/mvnw" in result["paths"]
    entry = result["commands"][0]
    assert entry["initializer"] == "spring-boot" and entry["command"].startswith("GET https://start.spring.io/")
    assert "start.spring.io" not in render_structure_for_display(store.get_workflow_state(CONV))   # not before the push
    call(tools, "push_repository_structure")
    text = render_structure_for_display(store.get_workflow_state(CONV))
    assert "spring-boot online initializer" in text and "Spring Boot 4.1.1" in text
    assert "[Spring Boot 4.1.1]" in rsa.scaffold_commit_message(result["commands"])


def test_scaffold_tool_requires_commands_or_initializers(db, ado):
    tools = created_repo(db, ado)
    result = call(tools, "scaffold_repository_structure")
    assert result["status"] == "error" and result["error_type"] == "VALIDATION_ERROR"


def test_prompt_uses_the_catalog_and_structure_options():
    from agent_framework.openai import OpenAIChatCompletionClient
    client = OpenAIChatCompletionClient(model="t", api_key="k", azure_endpoint="https://x.openai.azure.com",
                                        api_version="2024-12-01-preview")
    instructions = rsa.get_repo_setup_agent(client, CONV).default_options["instructions"]
    assert "[Scaffold catalog]" in instructions and "STRUCTURE_OPTIONS" in instructions
    assert "Install the toolchain" in instructions and "NEVER replace the stack" in instructions
    assert "Reference commands" not in instructions                          # moved to the generated catalog


# ---------------------------------------------------------------------------
# Live (opt-in): the real initializer services
# ---------------------------------------------------------------------------

live = pytest.mark.skipif(os.getenv("SCAFFOLD_LIVE_TESTS") != "1", reason="Set SCAFFOLD_LIVE_TESTS=1 to call the services")


@pytest.fixture
def real_network(monkeypatch):
    monkeypatch.setattr(init, "transport_factory", lambda: None)
    monkeypatch.undo()                     # undo no_real_network for these tests


@pytest.mark.integration
@live
@pytest.mark.parametrize("spec, expected", [
    ({"name": "spring-boot", "options": {"build": "maven", "dependencies": "web,data-jpa,postgresql"}},
     ["pom.xml", "mvnw"]),
    ({"name": "spring-boot", "options": {"build": "gradle-kotlin", "language": "kotlin"}}, ["build.gradle.kts"]),
    ({"name": "quarkus", "options": {"extensions": "rest,rest-jackson"}}, ["pom.xml"]),
    ({"name": "micronaut", "options": {"features": "data-jdbc"}}, ["build.gradle"]),
])
def test_live_initializers(real_network, tmp_path, spec, expected):
    run = init.run_initializer(spec, str(tmp_path), "live-demo")
    for name in expected:
        assert (tmp_path / name).exists(), name
    assert run.url.startswith("https://")


@pytest.mark.parametrize("output, name, expected", [
    ("Erlang/OTP 29 [erts-17.1] [64-bit]\n\nMix 1.20.1 (compiled with Erlang/OTP 28)", "mix",
     "Mix 1.20.1 (compiled with Erlang/OTP 28)"),
    ("------\nGradle 9.8.1\n------\n\nBuild time: 2026-10-07", "gradle", "Gradle 9.8.1"),
    ("\x1b[32mComposer\x1b[39m version \x1b[33m2.10.3\x1b[39m", "composer", "Composer version 2.10.3"),
    ("Apache Maven 3.10.0 (c43a36b)\nMaven home: C:\\maven", "mvn", "Apache Maven 3.10.0 (c43a36b)"),
    ("v3.268.0", "pulumi", "v3.268.0"),
    ("", "go", ""),
])
def test_version_line_picks_the_tools_own_version(output, name, expected):
    assert ss._version_line(output, name) == expected


def test_go_version_uses_its_subcommand():
    assert ss._VERSION_ARGS["go"] == ["version"]
