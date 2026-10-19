"""
Scaffold catalog: the single source of truth for every way DevFoundry can generate a repository structure.

- CLI generators: run by scaffold_service on the DevFoundry server (only those whose executable is installed are
  offered to the agent). Each entry says how to recognise the command, which flags make it headless (added
  automatically when missing), which files its output must contain, and a reference command for the agent.
- Online initializers (scaffold_initializers): official project generators served over HTTPS (Spring Initializr,
  Quarkus, Micronaut). They need no local toolchain and answer in about a second.

The agent's context is built from this catalog (describe_scaffold_catalog), so it only sees generators that can run,
and the stacks that cannot run are listed explicitly so the agent can offer alternatives instead of guessing.
"""
import fnmatch
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Generator:
    id: str
    ecosystem: str                         # shown to the agent / user, e.g. "Java / Kotlin (JVM)"
    title: str                             # e.g. "Vite (React, Vue, Svelte, Solid, Preact, Lit, vanilla)"
    executable: str                        # first argument of the command, e.g. "npx", "flutter"
    match: Tuple[str, ...]                 # how to recognise it: package name(s) or subcommand path
    example: str                           # reference command (adapt names / options)
    auto_flags: Tuple[str, ...] = ()       # appended when missing: make the generator headless / skip installs
    expected: Tuple[str, ...] = ()         # glob patterns the output must contain (searched below the command's cwd)
    templates: Tuple[str, ...] = ()        # known template names (guidance for the agent, not enforced)
    notes: str = ""
    verified: bool = False                 # output verified live by DevFoundry's tests


# Matching: for package runners (npx, uvx, npm/pnpm/yarn/bun create) `match` holds package names;
# for other executables it holds the subcommand path, e.g. ("new",) or ("package", "init").
GENERATORS: Tuple[Generator, ...] = (
    # ------------------------------------------------------------------ JavaScript / TypeScript
    Generator("vite", "JavaScript / TypeScript (web)", "Vite (React, Vue, Svelte, Solid, Preact, Lit, vanilla)",
              "npx", ("create-vite",),
              "npx --yes create-vite@latest <folder or .> --template react-ts",
              expected=("package.json", "vite.config.*", "index.html"),
              templates=("react-ts", "react", "react-swc-ts", "vue-ts", "vue", "svelte-ts", "svelte", "solid-ts",
                         "preact-ts", "lit-ts", "vanilla-ts", "vanilla"), verified=True),
    Generator("next", "JavaScript / TypeScript (web)", "Next.js", "npx", ("create-next-app",),
              "npx --yes create-next-app@latest <folder or .> --ts --eslint --app --src-dir --tailwind --import-alias @/* --use-npm",
              auto_flags=("--yes", "--skip-install", "--disable-git"),
              expected=("package.json", "next.config.*")),
    Generator("angular", "JavaScript / TypeScript (web)", "Angular", "npx", ("@angular/cli",),
              "npx --yes @angular/cli@latest new <name> --directory <folder or .>",
              auto_flags=("--defaults", "--skip-install", "--skip-git"), expected=("angular.json", "package.json")),
    Generator("vue", "JavaScript / TypeScript (web)", "Vue (create-vue)", "npx", ("create-vue",),
              "npx --yes create-vue@latest <folder or .> --typescript --router --pinia",
              expected=("package.json",)),
    Generator("sveltekit", "JavaScript / TypeScript (web)", "SvelteKit (sv)", "npx", ("sv",),
              "npx --yes sv@latest create <folder or .> --template minimal --types ts --no-add-ons",
              auto_flags=("--no-install",), expected=("package.json", "svelte.config.*")),
    Generator("nuxt", "JavaScript / TypeScript (web)", "Nuxt", "npx", ("nuxi", "create-nuxt"),
              "npx --yes nuxi@latest init <folder or .> --packageManager npm --gitInit false",
              auto_flags=("--no-install",), expected=("package.json", "nuxt.config.*")),
    Generator("astro", "JavaScript / TypeScript (web)", "Astro", "npx", ("create-astro",),
              "npx --yes create-astro@latest <folder or .> --template minimal",
              auto_flags=("--no-install", "--no-git", "--yes"), expected=("package.json", "astro.config.*")),
    Generator("react-router", "JavaScript / TypeScript (web)", "React Router (framework mode)", "npx",
              ("create-react-router", "create-remix"),
              "npx --yes create-react-router@latest <folder or .>",
              auto_flags=("--no-install", "--no-git-init", "--yes"), expected=("package.json",)),
    Generator("solid", "JavaScript / TypeScript (web)", "SolidStart", "npx", ("create-solid",),
              "npx --yes create-solid@latest <folder or .>", expected=("package.json",)),
    Generator("qwik", "JavaScript / TypeScript (web)", "Qwik", "npx", ("create-qwik",),
              "npx --yes create-qwik@latest empty <folder or .>", expected=("package.json",)),
    Generator("nest", "JavaScript / TypeScript (backend)", "NestJS", "npx", ("@nestjs/cli",),
              "npx --yes @nestjs/cli@latest new <name> --directory <folder or .> --package-manager npm",
              auto_flags=("--skip-install", "--skip-git"), expected=("package.json", "nest-cli.json")),
    Generator("express", "JavaScript / TypeScript (backend)", "Express (express-generator)", "npx",
              ("express-generator",), "npx --yes express-generator@latest --no-view <folder or .>",
              expected=("package.json", "app.js")),
    Generator("node-cli", "JavaScript / TypeScript (CLI / library)", "Node.js / TypeScript package (npm init + tsc)",
              "npm", ("init",),
              "npm init -y   then   npm install <deps> --package-lock-only   then   npx --yes -p typescript tsc --init",
              expected=("package.json",), verified=True),
    Generator("typescript", "JavaScript / TypeScript (CLI / library)", "TypeScript config (tsc --init)", "npx",
              ("typescript",), "npx --yes -p typescript tsc --init", expected=("tsconfig.json",)),
    Generator("t3", "JavaScript / TypeScript (full stack)", "T3 App (Next.js + tRPC + Prisma)", "npx",
              ("create-t3-app",), "npx --yes create-t3-app@latest <folder or .> --CI --trpc --prisma --tailwind",
              auto_flags=("--noInstall", "--noGit"), expected=("package.json",)),
    Generator("turbo", "JavaScript / TypeScript (full stack)", "Turborepo monorepo", "npx", ("create-turbo",),
              "npx --yes create-turbo@latest <folder or .> --package-manager npm",
              auto_flags=("--skip-install",), expected=("package.json", "turbo.json")),
    Generator("expo", "JavaScript / TypeScript (mobile)", "Expo (React Native)", "npx", ("create-expo-app",),
              "npx --yes create-expo-app@latest <folder or .> --template blank-typescript",
              auto_flags=("--no-install",), expected=("package.json", "app.json")),
    Generator("tauri", "JavaScript / TypeScript (desktop)", "Tauri", "npx", ("create-tauri-app",),
              "npx --yes create-tauri-app@latest <folder or .> --template react-ts --manager npm",
              auto_flags=("--yes",), expected=("package.json",)),
    Generator("electron", "JavaScript / TypeScript (desktop)", "Electron Forge", "npx", ("create-electron-app",),
              "npx --yes create-electron-app@latest <folder or .> --template=vite-typescript",
              expected=("package.json",)),
    Generator("aws-cdk", "Infrastructure as code", "AWS CDK", "npx", ("aws-cdk",),
              "npx --yes aws-cdk@latest init app --language typescript",
              auto_flags=("--generate-only",), expected=("cdk.json",)),
    Generator("bun", "JavaScript / TypeScript (Bun runtime)", "Bun", "bun", ("init",), "bun init --yes",
              auto_flags=("--yes",), expected=("package.json",), verified=True),
    Generator("deno", "JavaScript / TypeScript (Deno runtime)", "Deno", "deno", ("init",), "deno init <folder or .>",
              expected=("deno.json*",), verified=True),
    # ------------------------------------------------------------------ Python
    Generator("uv", "Python", "uv project (application, package or library)", "uv", ("init",),
              "uv init --package --vcs none --name <name> <folder or .>   then   uv add --no-sync <deps> (cwd = that folder)",
              expected=("pyproject.toml",), verified=True),
    Generator("uv-add", "Python", "uv add (dependencies of a uv project)", "uv", ("add",),
              "uv add --no-sync fastapi[standard]", auto_flags=("--no-sync",), verified=True),
    Generator("django", "Python", "Django", "uvx", ("django",),
              "uvx --from django django-admin startproject <name_with_underscores> <folder or .>",
              expected=("manage.py",)),
    Generator("poetry", "Python", "Poetry project", "uvx", ("poetry",),
              "uvx poetry new <name>", expected=("pyproject.toml",)),
    Generator("pyscaffold", "Python", "PyScaffold library", "uvx", ("pyscaffold",),
              "uvx --from pyscaffold putup <name>", expected=("setup.cfg",)),
    Generator("conan", "C / C++", "Conan + CMake (C/C++ executable or library)", "uvx", ("conan",),
              "uvx conan new cmake_exe -d name=<name> -d version=0.1   (cmake_lib for a library)",
              expected=("CMakeLists.txt", "conanfile.py")),
    # ------------------------------------------------------------------ .NET
    Generator("dotnet", ".NET (C#, F#, VB)", "dotnet new (webapi, console, classlib, blazor, mvc, worker, xunit, sln ...)",
              "dotnet", ("new",),
              "dotnet new webapi -o <folder> --no-restore   (console, classlib, blazor, mvc, worker, xunit; -lang F# for F#)",
              expected=("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "*.slnx"), verified=True),
    # ------------------------------------------------------------------ JVM (CLI; the online initializers need no JDK)
    Generator("maven", "Java / Kotlin (JVM)", "Maven archetype", "mvn", ("archetype:generate",),
              "mvn archetype:generate -DgroupId=com.example -DartifactId=<name> -DarchetypeArtifactId=maven-archetype-quickstart",
              auto_flags=("-B", "-DinteractiveMode=false"), expected=("pom.xml",), verified=True),
    Generator("gradle", "Java / Kotlin (JVM)", "Gradle init", "gradle", ("init",),
              "gradle init --type java-application --dsl kotlin --test-framework junit-jupiter --project-name <name> --package <pkg>",
              auto_flags=("--use-defaults", "--no-daemon"), expected=("settings.gradle*",), verified=True),
    # ------------------------------------------------------------------ Mobile / Dart
    # Platforms (decided D4): Android + iOS by default; add web only when the TRD mentions a browser/web version of the
    # app, desktop (windows, macos, linux) only when the TRD asks for a desktop app
    Generator("flutter", "Dart / Flutter (mobile, web, desktop)", "Flutter app", "flutter", ("create",),
              "flutter create --org com.example --project-name <name_with_underscores> --platforms=android,ios <folder or .>",
              auto_flags=("--no-pub", "--platforms=android,ios"), expected=("pubspec.yaml", "lib/main.dart"),
              notes="--platforms=android,ios by default; add web only if the TRD mentions a web/browser version, "
                    "desktop platforms only if the TRD asks for a desktop app", verified=True),
    Generator("dart", "Dart / Flutter (mobile, web, desktop)", "Dart (console, package, server)", "dart", ("create",),
              "dart create -t console <name>", auto_flags=("--no-pub",), expected=("pubspec.yaml",), verified=True),
    # ------------------------------------------------------------------ PHP / Ruby / Elixir / Go / Rust / Swift
    Generator("composer", "PHP", "Composer create-project (Laravel, Symfony, Slim, CakePHP)", "composer",
              ("create-project",), "composer create-project laravel/laravel <folder or .>",
              auto_flags=("--no-install", "--no-scripts", "--no-interaction"), expected=("composer.json",), verified=True),
    Generator("rails", "Ruby", "Ruby on Rails", "rails", ("new",),
              "rails new <folder or .> --database=postgresql --css=tailwind",
              auto_flags=("--skip-bundle", "--skip-git"), expected=("Gemfile", "config/routes.rb")),
    Generator("bundle", "Ruby", "Bundler gem / Gemfile", "bundle", ("gem",), "bundle gem <name>",
              expected=("*.gemspec",)),
    Generator("mix", "Elixir", "Mix project (application or library)", "mix", ("new",),
              "mix new <name> --sup", auto_flags=(), expected=("mix.exs",), verified=True),
    # phx.new asks "Fetch and install dependencies?" and fails on a closed stdin, so --no-install is always added
    Generator("phoenix", "Elixir", "Phoenix web app / API", "mix", ("phx.new",),
              "mix phx.new <name_with_underscores>   (--no-html --no-assets for an API, --database sqlite3|postgres)",
              auto_flags=("--no-install",), expected=("mix.exs", "config/config.exs"), verified=True),
    Generator("go", "Go", "Go module", "go", ("mod",), "go mod init <module>   then   go get <deps>",
              expected=("go.mod",), verified=True),
    Generator("cargo", "Rust", "Cargo project", "cargo", ("new", "init"),
              "cargo init --name <name> --vcs none   then   cargo add <deps>", auto_flags=(), expected=("Cargo.toml",), verified=True),
    Generator("swift", "Swift", "Swift package", "swift", ("package",),
              "swift package init --type executable --name <name>", expected=("Package.swift",)),
    Generator("pulumi", "Infrastructure as code", "Pulumi", "pulumi", ("new",),
              "pulumi new typescript --name <name>", auto_flags=("--yes", "--generate-only"),
              expected=("Pulumi.yaml",), verified=True),
)

# Generator packages that npx / uvx / npm create / pnpm create|dlx / yarn create / bun create may download and run.
# Extend with SCAFFOLD_ALLOWED_PACKAGES="pkg-a,pkg-b" (comma separated).
DEFAULT_ALLOWED_PACKAGES = frozenset(
    name for g in GENERATORS if g.executable in ("npx", "uvx") for name in g.match
) | {"create-svelte", "gitignore"}

# composer create-project may only use these project skeletons
COMPOSER_PACKAGES = frozenset({"laravel/laravel", "symfony/skeleton", "symfony/website-skeleton", "slim/slim-skeleton",
                               "cakephp/app", "yiisoft/yii2-app-basic", "laminas/laminas-mvc-skeleton"})

# Executables and the subcommands they may use (None = package runner, checked against the package allow-list).
ALLOWED_COMMANDS: Dict[str, Optional[Tuple[str, ...]]] = {
    "npx": None,
    "npm": ("create", "init", "install", "i", "add", "pkg"),
    "pnpm": ("create", "dlx", "init", "add", "install"),
    "yarn": ("create", "init", "add"),
    "bun": ("init", "create", "add"),
    "deno": ("init", "add"),
    "uv": ("init", "add", "remove", "lock"),
    "uvx": None,
    "poetry": ("new", "init", "add"),
    "dotnet": ("new", "sln", "add"),
    "mvn": ("archetype:generate",),
    "gradle": ("init",),
    "flutter": ("create",),
    "dart": ("create",),
    "composer": ("create-project", "require"),
    "rails": ("new",),
    "bundle": ("gem", "init", "add"),
    "mix": ("new", "phx.new"),
    "go": ("mod", "get"),
    "cargo": ("new", "init", "add"),
    "swift": ("package",),
    "pulumi": ("new",),
}

# Executables that need extra environment variables passed through to the command.
EXTRA_ENV = ("JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_HOME", "GRADLE_USER_HOME", "FLUTTER_ROOT", "PUB_CACHE",
             "ANDROID_HOME", "ANDROID_SDK_ROOT", "COMPOSER_HOME", "GEM_HOME", "GEM_PATH", "RUBY_HOME", "MIX_HOME",
             "HEX_HOME", "ERL_LIBS", "DENO_DIR", "BUN_INSTALL", "PULUMI_HOME", "SWIFT_PATH")

_PACKAGE_RUNNERS = {"npx", "uvx"}


def find_generator(executable: str, args: Sequence[str], package: Optional[str] = None) -> Optional[Generator]:
    """The catalog entry of a validated command (executable + its arguments, or the package it runs)."""
    for generator in GENERATORS:
        if generator.executable != executable:
            continue
        if executable in _PACKAGE_RUNNERS:
            if package and package in generator.match:
                return generator
            continue
        positional = [a for a in args if not a.startswith("-")]
        if positional and positional[0].lower() in generator.match:
            return generator
    return None


def apply_auto_flags(generator: Optional[Generator], args: List[str]) -> List[str]:
    """Appends the generator's headless / skip-install flags that the command does not already have."""
    if not generator:
        return args
    present = {a.split("=", 1)[0] for a in args}
    return args + [flag for flag in generator.auto_flags if flag.split("=", 1)[0] not in present]


def missing_expected_files(root: str, generator: Generator) -> List[str]:
    """Expected patterns (any of the alternatives for .NET-style lists) that no generated file matches."""
    if not generator.expected:
        return []
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", ".venv")]
        rel = os.path.relpath(dirpath, root).replace("\\", "/")
        names += [(f if rel == "." else f"{rel}/{f}") for f in filenames]

    def found(pattern: str) -> bool:
        return any(fnmatch.fnmatch(n, pattern) or fnmatch.fnmatch(n, f"*/{pattern}") for n in names)

    if generator.id == "dotnet":            # any one project/solution file is enough
        return [] if any(found(p) for p in generator.expected) else [" or ".join(generator.expected)]
    return [p for p in generator.expected if not found(p)]


def describe_scaffold_catalog(installed: Iterable[str], initializers: Sequence[Dict[str, str]]) -> str:
    """
    The scaffold options the agent may use, built from this catalog: available CLI generators (grouped by
    ecosystem, with a reference command and known templates), the online initializers, and the stacks whose
    toolchain is not installed (so the agent can offer alternatives instead of guessing).
    """
    installed = set(installed)
    available = [g for g in GENERATORS if g.executable in installed]
    unavailable = [g for g in GENERATORS if g.executable not in installed]
    lines = ["[Scaffold catalog] Only these generators can run. Use scaffold_repository_structure.",
             "CLI generators (commands):"]
    by_ecosystem: Dict[str, List[Generator]] = {}
    for g in available:
        by_ecosystem.setdefault(g.ecosystem, []).append(g)
    for ecosystem, gens in by_ecosystem.items():
        lines.append(f"* {ecosystem}:")
        for g in gens:
            extra = f" [templates: {', '.join(g.templates)}]" if g.templates else ""
            lines.append(f"  - {g.title}: `{g.example}`{extra}")
    lines.append("Online initializers (`initializers` argument; official generators, no local toolchain, ~1 s):")
    for init in initializers:
        lines.append(f"  - {init['name']}: {init['title']}. options: {init['options']}")
    missing = sorted({g.ecosystem + f" ({g.executable})" for g in unavailable})
    if missing:
        lines.append("NOT available on this server (toolchain not installed): " + "; ".join(missing))
    return "\n".join(lines)
