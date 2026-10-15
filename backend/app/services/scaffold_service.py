"""
Command-based repository scaffolding.

The Repo Setup Agent chooses the official generator / init commands for the approved tech stack (for example
`npx --yes create-vite@latest frontend --template react-ts` or `uv init --package --vcs none --name my-cli .`).
This module runs those commands - and nothing else - in an empty temporary folder that stands for the repository
root, then reads back every file they produced, byte for byte. It never writes file content itself: the structure
shown to the user (and later pushed) is exactly the generators' output.

Safety: commands are parsed into an argument list and run without a shell; only allow-listed executables,
subcommands and generator packages are accepted; arguments may not contain shell metacharacters, absolute paths or
'..'; the process gets a minimal environment (no PAT, Supabase or OpenAI secrets) and a timeout; symlinks are not
followed, so nothing outside the temporary folder can be read back.
"""
import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.core.logger import logger
from app.services.mcp.ado_git_push import AdoPushError, Base64Content, MAX_FILE_BYTES, MAX_FILES
from app.services.repo_structure import normalize_structure
from app.services.scaffold_catalog import (ALLOWED_COMMANDS, COMPOSER_PACKAGES, DEFAULT_ALLOWED_PACKAGES, EXTRA_ENV,
                                           GENERATORS, apply_auto_flags, describe_scaffold_catalog, find_generator,
                                           missing_expected_files)
from app.services.scaffold_initializers import describe_initializers, run_initializer

MAX_COMMANDS = 8
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600     # first runs of Maven, Gradle, Pulumi and `go get` download caches
OUTPUT_TAIL_CHARS = 1500

# The allowed executables, subcommands and generator packages come from the scaffold catalog
# (scaffold_catalog.ALLOWED_COMMANDS / DEFAULT_ALLOWED_PACKAGES); extend packages with SCAFFOLD_ALLOWED_PACKAGES.
VERSION_LOOKUP_TIMEOUT_SECONDS = 4.0

# Folders that installers / tools create but that never belong in a repository.
IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                "obj", ".pnpm-store", ".gradle", ".dart_tool", ".angular", ".next", ".nuxt", ".svelte-kit", ".turbo"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}

# One argument (after shlex parsing): no whitespace, quotes or shell metacharacters (& | ; < > ` $ % ^ ! ( ) etc.).
_SAFE_ARG = re.compile(r"^[A-Za-z0-9@._/:=+,*#\[\]-]+$")
_SAFE_CWD = re.compile(r"^[A-Za-z0-9._/-]+$")

# Environment variables passed to the commands (everything else, including all secrets, is dropped).
_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "USER", "LOGNAME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "DOTNET_ROOT", "GOPATH", "GOROOT", "GOMODCACHE", "CARGO_HOME", "RUSTUP_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "NPM_CONFIG_CACHE", "NPM_CONFIG_REGISTRY",
} | set(EXTRA_ENV)
_NON_INTERACTIVE_ENV = {
    "CI": "true", "npm_config_yes": "true", "npm_config_fund": "false", "npm_config_audit": "false",
    "npm_config_update_notifier": "false", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "UV_NO_PROGRESS": "1",
    "PYTHONUTF8": "1", "DEBIAN_FRONTEND": "noninteractive",
}


@dataclass
class ScaffoldCommand:
    command: str
    cwd: str = "."
    generator: Optional[str] = None      # scaffold catalog id, e.g. "vite"
    version: Optional[str] = None        # version actually used, e.g. "create-vite 7.1.2"

    def to_dict(self) -> Dict[str, str]:
        data = {"command": self.command, "cwd": self.cwd}
        if self.version:
            data["version"] = self.version
        return data


@dataclass
class ScaffoldResult:
    files: List[Dict[str, str]]
    commands: List[Any]                  # ScaffoldCommand / InitializerRun (both have to_dict) in execution order
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, List[str]] = field(default_factory=dict)
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)


def _error(message: str, error_type: str, details: str = "") -> AdoPushError:
    return AdoPushError(message, error_type=error_type, details=details[-OUTPUT_TAIL_CHARS:])


def command_timeout_seconds() -> int:
    try:
        return max(10, int(os.getenv("SCAFFOLD_COMMAND_TIMEOUT_SECONDS") or DEFAULT_COMMAND_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS


def allowed_packages() -> set:
    extra = {p.strip().lower() for p in (os.getenv("SCAFFOLD_ALLOWED_PACKAGES") or "").split(",") if p.strip()}
    return DEFAULT_ALLOWED_PACKAGES | extra


# ---------------------------------------------------------------------------
# Toolchains available on this server (shown to the agent so it only picks commands that can run)
# ---------------------------------------------------------------------------

def _resolve_executable(name: str) -> Optional[str]:
    return shutil.which(name)


_VERSION_ARGS = {"go": ["version"]}            # executables without a `--version` flag
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _version_line(output: str, name: str = "") -> str:
    """
    The line that carries the tool's version: the first one starting with the tool's name (Mix prints an Erlang banner
    first), else the first one with a digit (skips banners such as Gradle's dashes). Colour codes are stripped.
    """
    lines = [line.strip() for line in _ANSI_ESCAPE.sub("", output or "").splitlines()]
    lines = [line for line in lines if line and re.search(r"\d", line)]
    own = [line for line in lines if name and line.lower().startswith(name.lower())]
    return (own or lines or [""])[0][:60]


@lru_cache(maxsize=1)
def available_toolchains() -> Dict[str, str]:
    """{executable: version} for the allow-listed executables installed on this server (cached per process)."""
    tools: Dict[str, str] = {}
    for name in ALLOWED_COMMANDS:
        path = _resolve_executable(name)
        if not path:
            continue
        version = ""
        try:
            proc = subprocess.run([path] + _VERSION_ARGS.get(name, ["--version"]), capture_output=True, text=True,
                                  timeout=60, stdin=subprocess.DEVNULL, env=_command_env(), encoding="utf-8",
                                  errors="replace")      # JVM tools can take a while to start
            version = _version_line(proc.stdout or proc.stderr, name) if proc.returncode == 0 else ""
        except Exception:
            pass
        tools[name] = version or "installed"
    return tools


def describe_toolchains() -> str:
    """The scaffold catalog for the agent: available generators, online initializers and missing toolchains."""
    return describe_scaffold_catalog(available_toolchains().keys(), describe_initializers())


# ---------------------------------------------------------------------------
# Command validation
# ---------------------------------------------------------------------------

def _package_name(spec: str) -> str:
    """'create-vite@latest' -> 'create-vite', '@angular/cli@19' -> '@angular/cli', 'fastapi[standard]' -> 'fastapi'."""
    spec = spec.strip().lower()
    if spec.startswith("@"):
        scope, _, rest = spec.partition("/")
        return f"{scope}/{rest.split('@', 1)[0]}"
    return re.split(r"[@=\[<>~!]", spec, maxsplit=1)[0]


def _initializer_package(name: str) -> str:
    """`npm create vite` / `npm init vite` / `yarn create vite` run the package create-vite (scoped: @x -> @x/create)."""
    name = _package_name(name)
    if name.startswith("@"):
        scope, _, rest = name.partition("/")
        return f"{scope}/create-{rest}" if rest else f"{scope}/create"
    return name if name.startswith("create-") else f"create-{name}"


def _first_positional(args: List[str], package_options: Iterable[str] = (),
                      value_options: Iterable[str] = ()) -> Tuple[Optional[str], List[str]]:
    """First non-option argument (before it, options are skipped) and every value given to a package option."""
    package_opts, value_opts = set(package_options), set(value_options)
    packages: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            return arg, packages
        name, eq, value = arg.partition("=")
        if name in package_opts or name in value_opts:
            if not eq:
                index += 1
                value = args[index] if index < len(args) else ""
            if name in package_opts:
                packages.append(value)
        index += 1
    return None, packages


def _unsafe_path(arg: str) -> bool:
    """URL, absolute path (also as an option value, e.g. --prefix=/x), home path or '..' anywhere."""
    if "://" in arg or ".." in arg:
        return True
    return any(part.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", part) for part in re.split(r"[=,]", arg))


def _check_package(package: Optional[str], executable: str) -> None:
    if not package:
        raise _error(f"`{executable}` needs a generator package to run.", "COMMAND_NOT_ALLOWED")
    name = _package_name(package)
    if name not in allowed_packages():
        raise _error(f"Generator package '{name}' is not on the allow-list. Allowed: {', '.join(sorted(allowed_packages()))}. "
                     "Add it to SCAFFOLD_ALLOWED_PACKAGES if it is trusted.", "COMMAND_NOT_ALLOWED")


def parse_command(spec: Any) -> Tuple[ScaffoldCommand, List[str]]:
    """Validates one command ({"command", "cwd"} or a string). Returns the command and its argument list."""
    data = spec if isinstance(spec, Mapping) else (spec.model_dump() if hasattr(spec, "model_dump") else {"command": spec})
    raw = str(data.get("command") or "").strip()
    cwd = str(data.get("cwd") or ".").strip().replace("\\", "/").strip("/") or "."
    if not raw:
        raise _error("A scaffold command must not be empty.", "VALIDATION_ERROR")
    if any(ch in raw for ch in "\n\r\0"):
        raise _error("A scaffold command must be a single line.", "VALIDATION_ERROR")
    try:
        args = shlex.split(raw, posix=True)
    except ValueError as e:
        raise _error(f"Cannot parse command `{raw}`: {e}", "VALIDATION_ERROR")
    for arg in args:
        if not _SAFE_ARG.match(arg):
            raise _error(f"Argument '{arg}' in `{raw}` contains characters that are not allowed (no shell operators, "
                         "quotes, spaces, '$', '%', '!', '^', '<', '>', '~'). Run one command per entry and use 'cwd' "
                         "instead of 'cd'.", "COMMAND_NOT_ALLOWED")
        if _unsafe_path(arg):
            raise _error(f"Argument '{arg}' in `{raw}` must not be a URL, an absolute path or contain '..'.",
                         "COMMAND_NOT_ALLOWED")
    if cwd != "." and (not _SAFE_CWD.match(cwd) or ".." in cwd.split("/")):
        raise _error(f"Working folder '{cwd}' must be a relative folder inside the repository.", "COMMAND_NOT_ALLOWED")

    executable = re.sub(r"\.(cmd|exe|bat|ps1)$", "", args[0].lower())
    if executable not in ALLOWED_COMMANDS:
        raise _error(f"`{args[0]}` is not an allowed scaffold executable. Allowed: {', '.join(ALLOWED_COMMANDS)}.",
                     "COMMAND_NOT_ALLOWED")
    rest = args[1:]
    subcommands = ALLOWED_COMMANDS[executable]
    generator = None
    if executable in ("npx", "uvx"):
        if executable == "npx":
            package, packages = _first_positional(rest, ("-p", "--package"), ("--cache", "--registry"))
        else:
            if any(arg.split("=", 1)[0] in ("--with", "-w", "--with-requirements", "--with-editable") for arg in rest):
                raise _error("`uvx --with` is not allowed (it installs extra packages).", "COMMAND_NOT_ALLOWED")
            package, packages = _first_positional(rest, ("--from",), ("--python", "-p", "--index", "--index-url"))
        for name in packages or [package]:
            _check_package(name, executable)
        runs = _package_name(packages[0] if packages else package)
        generator = find_generator(executable, [], runs)
        if generator and package in args:
            # headless flags belong to the generator (after the package / binary), not to npx / uvx itself
            split = args.index(package) + 1
            args = args[:split] + apply_auto_flags(generator, args[split:])
    else:
        if not rest or rest[0].lower() not in subcommands:
            raise _error(f"`{executable} {rest[0] if rest else ''}` is not allowed. Allowed subcommands: "
                         f"{', '.join(subcommands)}.", "COMMAND_NOT_ALLOWED")
        sub = rest[0].lower()
        if executable == "dotnet" and sub == "new" and len(rest) > 1 and rest[1].lower() in ("install", "uninstall"):
            raise _error("Installing dotnet templates is not allowed; use a built-in template.", "COMMAND_NOT_ALLOWED")
        if executable in ("npm", "pnpm", "yarn", "bun") and sub in ("create", "init", "dlx"):
            target, _ = _first_positional(rest[1:])
            if sub == "dlx":
                _check_package(target, f"{executable} dlx")
                generator = find_generator("npx", [], _package_name(target))
            elif target and not (executable == "bun" and sub == "init"):   # `npm init -y` / `bun init`: none
                _check_package(_initializer_package(target), f"{executable} {sub}")
                generator = find_generator("npx", [], _initializer_package(target))
        if executable == "composer" and sub == "create-project":
            target, _ = _first_positional(rest[1:])
            if not target or target.lower().split(":")[0] not in COMPOSER_PACKAGES:
                raise _error(f"composer create-project may only use: {', '.join(sorted(COMPOSER_PACKAGES))}.",
                             "COMMAND_NOT_ALLOWED")
        if executable == "swift" and (len(rest) < 2 or rest[1].lower() != "init"):
            raise _error("Only `swift package init` is allowed.", "COMMAND_NOT_ALLOWED")
        if executable in ("npm", "pnpm") and sub in ("install", "i", "add") and "--ignore-scripts" not in rest:
            args.append("--ignore-scripts")   # never run dependency install scripts on the server
        if generator is None:
            generator = find_generator(executable, rest)
            args = [args[0]] + apply_auto_flags(generator, args[1:])
    args[0] = executable
    return ScaffoldCommand(command=raw, cwd=cwd, generator=generator.id if generator else None), args


def _command_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOWLIST}
    env.update(_NON_INTERACTIVE_ENV)
    return env


# ---------------------------------------------------------------------------
# Reading the generated files back
# ---------------------------------------------------------------------------

def _inside(root: str, path: str) -> bool:
    root_real = os.path.realpath(root)
    return os.path.commonpath([root_real, os.path.realpath(path)]) == root_real


def collect_files(root_dir: str) -> Tuple[List[Dict[str, str]], Dict[str, List[str]]]:
    """
    Reads every generated file below `root_dir` exactly as written. Text stays text (UTF-8), anything else is
    base64-encoded binary. Skips installer/tool folders (node_modules, .venv, .git, ...) and symlinks. Empty folders
    cannot exist in Git and are reported, not filled with placeholders.
    """
    files: List[Dict[str, str]] = []
    skipped: Dict[str, List[str]] = {"ignored_folders": [], "empty_folders": [], "symlinks": [], "too_large": []}
    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_dir).replace("\\", "/")
        kept = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            rel = (name if rel_dir == "." else f"{rel_dir}/{name}")
            if os.path.islink(full):
                skipped["symlinks"].append("/" + rel)
            elif name in IGNORED_DIRS:
                skipped["ignored_folders"].append("/" + rel)
            else:
                kept.append(name)
        dirnames[:] = kept
        if rel_dir != "." and not filenames and not kept:
            skipped["empty_folders"].append("/" + rel_dir)

        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = "/" + (name if rel_dir == "." else f"{rel_dir}/{name}")
            if name in IGNORED_FILES:
                continue
            if os.path.islink(full) or not _inside(root_dir, full):
                skipped["symlinks"].append(rel)
                continue
            size = os.path.getsize(full)
            if size > MAX_FILE_BYTES:
                skipped["too_large"].append(f"{rel} ({size} bytes)")
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            try:
                if b"\x00" in data:
                    raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
                files.append({"path": rel, "content": data.decode("utf-8")})
            except UnicodeDecodeError:
                files.append({"path": rel, "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
    return files, {k: v for k, v in skipped.items() if v}


# ---------------------------------------------------------------------------
# Running the commands
# ---------------------------------------------------------------------------

def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-OUTPUT_TAIL_CHARS:]


def _registry_version(runner: str, spec: str) -> Optional[str]:
    """Version a package runner will use: the pinned one, or the registry's latest (best effort, no failure)."""
    name = _package_name(spec)
    pinned = spec.strip()[len(name):].lstrip("@=").split("[")[0]       # 'create-vite@7.1.0' -> '7.1.0'
    if pinned and pinned.lower() != "latest":
        return f"{name} {pinned}"
    try:
        import httpx
        url = (f"https://registry.npmjs.org/{name.replace('/', '%2f')}/latest" if runner == "npx"
               else f"https://pypi.org/pypi/{name}/json")
        with httpx.Client(timeout=VERSION_LOOKUP_TIMEOUT_SECONDS) as client:
            data = client.get(url).json()
        version = data.get("version") if runner == "npx" else data.get("info", {}).get("version")
        return f"{name} {version}" if version else None
    except Exception:
        return None


def _command_version(command: ScaffoldCommand, args: List[str]) -> Optional[str]:
    if args[0] in ("npx", "uvx"):
        package, packages = _first_positional(args[1:], ("-p", "--package", "--from"),
                                              ("--cache", "--registry", "--python", "--index", "--index-url"))
        spec = packages[0] if packages else package
        return _registry_version(args[0], spec) if spec else None
    tool = available_toolchains().get(args[0])
    return f"{args[0]} {tool}" if tool and tool != "installed" and not tool.lower().startswith(args[0]) else tool


def run_scaffold_commands(commands: Iterable[Any], timeout_seconds: Optional[int] = None,
                          initializers: Optional[Iterable[Any]] = None, default_name: str = "app") -> ScaffoldResult:
    """
    Validates and runs the online initializers and then the commands, in order, inside a fresh temporary folder (the
    repository root), and returns the generated files as a validated structure. Raises AdoPushError (error_type
    COMMAND_NOT_ALLOWED, TOOL_NOT_INSTALLED, COMMAND_FAILED, COMMAND_TIMEOUT, INITIALIZER_FAILED,
    INITIALIZER_UNAVAILABLE, SCAFFOLD_EMPTY or VALIDATION_ERROR); nothing is invented as a fallback.
    """
    specs = list(commands or [])
    inits = [i if isinstance(i, Mapping) else (i.model_dump() if hasattr(i, "model_dump") else {}) for i in initializers or []]
    if not specs and not inits:
        raise _error("At least one scaffold command or initializer is required.", "VALIDATION_ERROR")
    if len(specs) + len(inits) > MAX_COMMANDS:
        raise _error(f"Too many scaffold steps ({len(specs) + len(inits)}); the limit is {MAX_COMMANDS}.",
                     "VALIDATION_ERROR")
    parsed = [parse_command(spec) for spec in specs]          # validate everything before running anything
    for command, args in parsed:
        if not _resolve_executable(args[0]):
            installed = ", ".join(name for name in ALLOWED_COMMANDS if _resolve_executable(name)) or "none"
            raise _error(f"`{args[0]}` is not installed on the DevFoundry server, so `{command.command}` cannot run. "
                         f"Installed scaffold tools: {installed}. Java/Kotlin projects can use the online initializers "
                         "(spring-boot, quarkus, micronaut) instead.", "TOOL_NOT_INSTALLED")

    timeout = timeout_seconds or command_timeout_seconds()
    env = _command_env()
    started = time.monotonic()
    outputs: List[Dict[str, Any]] = []
    warnings: List[str] = []
    executed: List[Any] = []
    with tempfile.TemporaryDirectory(prefix="devfoundry-scaffold-") as workspace:
        for spec in inits:
            executed.append(run_initializer(spec, workspace, default_name))
        for command, args in parsed:
            cwd = os.path.join(workspace, *([] if command.cwd == "." else command.cwd.split("/")))
            os.makedirs(cwd, exist_ok=True)
            logger.info(f"[Scaffold] Running `{' '.join(args)}` in /{'' if command.cwd == '.' else command.cwd}")
            try:
                proc = subprocess.run([_resolve_executable(args[0]), *args[1:]], cwd=cwd, env=env, shell=False,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise _error(f"`{command.command}` did not finish within {timeout} seconds.", "COMMAND_TIMEOUT",
                             _tail(str(e.stdout or "")))
            except OSError as e:
                raise _error(f"`{command.command}` could not be started: {e}", "COMMAND_FAILED")
            output = _tail((proc.stdout or "") + "\n" + (proc.stderr or ""))
            outputs.append({"command": command.command, "cwd": command.cwd, "exit_code": proc.returncode})
            if proc.returncode != 0:
                logger.warning(f"[Scaffold] `{command.command}` exited with {proc.returncode}")
                raise _error(f"`{command.command}` failed with exit code {proc.returncode}.", "COMMAND_FAILED", output)
            generator = next((g for g in GENERATORS if g.id == command.generator), None)
            missing = missing_expected_files(cwd, generator) if generator else []
            if missing:
                warnings.append(f"`{command.command}` (in /{'' if command.cwd == '.' else command.cwd}) did not "
                                f"produce the expected {', '.join(missing)}; check the generator options.")
            command.version = _command_version(command, args)
            executed.append(command)

        files, skipped = collect_files(workspace)

    if not files:
        raise _error("The scaffold commands ran but produced no files.", "SCAFFOLD_EMPTY")
    if len(files) > MAX_FILES:
        raise _error(f"The scaffold commands produced {len(files)} files; the limit is {MAX_FILES}. "
                     "Use generator options that skip installing dependencies.", "VALIDATION_ERROR")
    structure = normalize_structure(files, allow_empty=True)
    duration = round(time.monotonic() - started, 1)
    logger.info(f"[Scaffold] {len(executed)} step(s) produced {len(structure)} files in {duration}s")
    return ScaffoldResult(files=structure, commands=executed, outputs=outputs, skipped=skipped,
                          duration_seconds=duration, warnings=warnings)


def decode_file(item: Mapping[str, str]) -> bytes:
    """Bytes of a structure file (binary files are stored base64-encoded)."""
    if item.get("encoding") == "base64":
        return base64.b64decode(item["content"])
    return str(item.get("content") or "").encode("utf-8")


__all__ = ["ScaffoldCommand", "ScaffoldResult", "run_scaffold_commands", "parse_command", "collect_files",
           "available_toolchains", "describe_toolchains", "decode_file", "Base64Content"]
