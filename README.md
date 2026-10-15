# DevFoundry

Requirement-to-delivery agent. The frontend is a React (Vite + TypeScript) app with Microsoft Entra sign-in. The backend is a FastAPI app that serves conversation and chat endpoints, backed by Supabase and an Azure OpenAI multi-agent pipeline.

# Prerequisites

| Tool | Version |
| --- | --- |
| Python | 3.12+ |
| Node.js | 18+ |
| npm | 9+ |
| uv | 0.5+ (scaffold commands for Python stacks) |

The backend server runs the repository generators itself (see "Repository structure from generator commands"), so the
CLIs for the stacks you want to generate must be installed **on the backend host**: Node.js/npm (`npx`) for JavaScript and
TypeScript stacks, `uv` for Python stacks, and optionally the .NET SDK, Go or Cargo.

# Environment Setup

All credentials live in a **single `.env` file at the repo root**. It is shared by both the backend and the frontend.

```powershell
# From the repo root
copy .env.example .env
```

Then fill in `.env`:

| Variable | Description | Required |
| --- | --- | --- |
| `VITE_AZURE_CLIENT_ID` | Entra app registration client ID | Yes (for sign-in) |
| `VITE_AZURE_TENANT_ID` | Entra tenant ID | Yes (for sign-in) |
| `VITE_AZURE_REDIRECT_URI` | `http://localhost:5173/auth-callback` | Yes |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Yes |
| `SUPABASE_URL` | Supabase project URL | Yes |
| `SUPABASE_KEY` | Supabase **service-role** key (backend only). Required: after migration 003 the tables reject the anon key | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | Optional, used instead of `SUPABASE_KEY` when set | Optional |
| `AZURE_AD_TENANT_ID` / `AZURE_AD_CLIENT_ID` | Tenant and client id used to validate sign-in tokens. They default to the `VITE_AZURE_*` values | Optional |
| `AZURE_OPENAI_APIKEY` | Azure OpenAI API key | Yes (chat replies fail without it) |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint URL | Yes (chat replies fail without it) |
| `AZURE_OPENAI_VERSION` | API version (default: `2024-12-01-preview`) | Optional |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (default: `gpt-4`) | Optional |
| `LOCAL_SLM_API_KEY` | Local SLM API key (automatic fallback if Azure OpenAI fails) | Optional |
| `LOCAL_SLM_BASE_URL` | Local SLM endpoint (default: `https://llm.maqsoftware.net/v1`) | Optional |
| `LOCAL_SLM_MODEL` | Local SLM model name (default: `qwen-3.8-27b`) | Optional |
| `GUARDRAILS_API_KEY` | Guardrails SLM API key (defaults to `LOCAL_SLM_API_KEY` / `MAQ_AI_API_KEY`) | Optional |
| `GUARDRAILS_BASE_URL` | Guardrails SLM endpoint (default: `https://llm.maqsoftware.net/v1`) | Optional |
| `GUARDRAILS_MODEL` | Guardrails SLM model name (default: `qwen-3.8-27b`) | Optional |
| `GUARDRAILS_ALLOWED_DOMAINS` | Comma-separated domain allowlist for URL filter | Optional |
| `AZURE_DEVOPS_ORG_NAME` | Azure DevOps organization name or URL | Yes (for ADO project & repo creation) |
| `LLM_PROVIDER_ORDER` | Fallback order of the LLM providers (default `azure_openai,maq_ai,groq`): MAQ AI takes the load when Azure is unavailable, Groq (small free-tier quotas) is the last resort | Optional |
| `MAQ_AI_API_KEY` / `MAQ_AI_BASE_URL` / `MAQ_AI_MODEL` | First fallback (internal SLM gateway, `qwen-3.8-27b`, 131k-token context). Requests are streamed so slow answers are not cut off by the gateway's 120 s limit | Optional |
| `GROQ_API_KEY` / `GROQ_MODEL` | Last fallback. `GROQ_MODEL` may list several models, tried in order (default `openai/gpt-oss-20b,openai/gpt-oss-120b`; each has its own free-tier quota) | Optional |
| `CHAT_HISTORY_TOKEN_BUDGET` | How much of the conversation every agent receives (default `60000` tokens). Messages are never cut; the whole project chat is one session for the model, and only very long projects drop their oldest messages (the stored PRD/TRD are always included) | Optional |
| `<PROVIDER>_TIMEOUT_SECONDS` / `<PROVIDER>_MAX_RETRIES` | Per-provider limits, `<PROVIDER>` = `AZURE_OPENAI` (120 s, 1 retry), `GROQ` (60 s, 2), `MAQ_AI` (900 s, 0). `GROQ_MAX_REQUEST_TOKENS` (8000): for larger requests Groq gets the newest messages that fit | Optional |
| `AZURE_DEVOPS_PAT` | Azure DevOps Personal Access Token (PAT) with project, code and work items read & write scopes | Yes (for ADO project, repo and work item creation) |

> **Without Supabase credentials**, the backend uses an in-memory conversation store (resets on restart).  
> **If Azure OpenAI encounters any errors or credentials fail**, the chat service automatically falls back to Local SLM (`LOCAL_SLM_API_KEY`).  
> **Multi-Agent Guardrails Layer:** User inputs and agent outputs are validated via `openai-guardrails` against the local SLM with a fail-closed policy. PII is automatically redacted and external URLs are restricted to allowed domains.

**LLM fallback:** Azure OpenAI → Groq models (in `GROQ_MODEL` order) → MAQ AI. A provider that fails is skipped for 3 minutes.
Users only ever see a short "AI service busy or unavailable" message with a reference id; the backend log shows, under
the same reference, why each provider failed (e.g. `HTTP 403: … firewall`, `HTTP 429: rate limit …`, `skipped (request
~9500 tokens … exceeds its 8000-token request limit)`).

Register `http://localhost:5173/auth-callback` as a **Single-page application** redirect URI on the Entra app registration.

---

# Getting Started

Run the backend and the frontend in **two separate terminals**.

## Backend — Terminal 1

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirement.txt
uvicorn app.main:app --reload --port 8000
```

The API is live at `http://localhost:8000`. Interactive Swagger docs are at `http://localhost:8000/docs`.

## Frontend — Terminal 2

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173` in your browser.

---

# API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Returns `{ "status": "ok" }` |
| GET | `/api/conversation` | Lists all conversations owned by the user (`conversation_id`, `title`) |
| POST | `/api/conversation` | Creates a new conversation; returns `{ "conversation_id", "title" }` |
| POST | `/api/chat` | Accepts JSON `{ "conversation_id", "user_query" }` or multipart form-data with an optional `file` field; returns `{ "conversation_id", "response" }` |
| GET | `/api/projects` | Lists all projects owned by the signed-in user (`conversation_id`, `project_name`, `prd`, `trd`, etc.) |

`POST /api/chat` returns 404 when `conversation_id` does not exist.

---

# Sign-in, Ownership and Database Security

- **Sign-in on every API call:** the frontend sends the user's Microsoft Entra ID token (MSAL) as `Authorization: Bearer <token>` on every `/api/*` call.
- **Token validation:** the backend (`backend/app/core/auth.py`) validates it: signature against the tenant's keys, issuer, audience (the app's client id) and expiry. It identifies the user by the immutable Entra object id (`oid`).
- **Ownership:** each conversation belongs to the user who created it. The API lists only your own conversations, and another user's conversation returns **404**. `/health` stays public.
- **Project ownership:** projects are linked to conversations via `conv_id`. `GET /api/projects` returns only the projects associated with the signed-in user's conversations.
- **User table:** signed-in users are recorded in the `AppUser` table (`user_id` = `oid`, `username`, `display_name`, `email`, `last_seen_at`).
- **Table links:** `Project.conv_id` references `Conversation`, so deleting a conversation deletes its `Project` and `Repository` rows. Nothing is deleted in Azure DevOps.
- **Database access:** the tables are closed to Supabase's anon/authenticated keys. Only the backend, using the **service-role key**, can read and write them.

Database migrations, run in this order in the Supabase SQL editor:
1. `backend/db/migrations/001_create_project_and_repository.sql`
2. `backend/db/migrations/002_document_versions.sql`
3. `backend/db/migrations/003_users_and_ownership.sql`: the `AppUser` table, the Conversation → Project link and the database hardening. **Switch the backend to the service-role key first.**
4. `backend/db/migrations/004_assign_existing_conversations.sql`: assigns the old `default_user` conversations to one account and makes the owner mandatory. Set your email in the file and run it **after signing in once**.
5. `backend/db/migrations/005_scaffold_commands.sql`: stores the generator commands and their exact output (`Repository.scaffold_commands`, `Repository.scaffold_structure`). Without it, the generated files and the requested changes are pushed as one commit.
6. `backend/db/migrations/006_work_items.sql`: the Azure Boards work items draft and the created items (`Project.work_items_proposal`, `Project.work_items` and their PRD versions). Without it, the work items step answers with a request to run this migration.

# Azure DevOps Repository Setup

After the PRD and TRD are approved, the Repo Setup Agent works through these steps. Each step waits for the user's explicit approval.

1. **Project proposal:** the agent proposes an ADO project name and description from the PRD and TRD. The user can change them as often as needed.
2. **Create project:** the agent creates the ADO project, waits until ADO has finished provisioning it, and saves the real project id. It then suggests a repository name.
3. **Repository name:** the user can change the suggested name. The TRD isn't re-opened at this point.
4. **Create repository:** the agent creates an empty repository, then generates its structure by **running the stack's official generator commands** (see below) and shows the commands and the resulting tree.
5. **Structure review:** the user can
   - approve it,
   - ask for specific extra files or folders (they are layered on top of the generated files and marked "(added)"), or
   - reject the approach / ask for another stack, framework or template: the TRD is updated and the structure is generated again with new commands. Requested files that still fit are re-added.
6. **Push:** the first push commits the generator output exactly as produced (commit 1, its message lists the commands), then the requested changes (commit 2, only if there are any). Later changes are pushed as one follow-up commit with only the differences.
7. **Azure Boards work items** (any time after the project exists): the Backlog agent drafts one Epic, Features and one story per PRD requirement (FR-001…) with acceptance criteria. After review and explicit approval, the work items are created in the project's Azure Boards with the project's own types (Scrum: Product Backlog Item, Agile: User Story, Basic: Issue) and parent links. When the PRD changes, the work items are flagged as out of date; syncing again adds new items and updates changed ones, and never deletes or duplicates.

The structure shown in chat and the files pushed are guaranteed to be the same. Both come from the `Repository` row in Supabase: the tree is drawn by code, and the push tool reads that row instead of taking files from the model.

## Repository structure from generator commands

The Repo Setup Agent reads the TRD and the CLIs installed on the server, and chooses commands such as:

| Stack | Commands |
| --- | --- |
| Python CLI | `uv init --package --vcs none --name <repo> .` then `uv add --no-sync typer` |
| React + FastAPI | `npx --yes create-vite@latest frontend --template react-ts`, `uv init --app --vcs none --name backend backend`, `uv add --no-sync fastapi[standard]` (in `backend/`) |
| Next.js / Angular / NestJS / .NET | `create-next-app`, `@angular/cli new`, `@nestjs/cli new`, `dotnet new ...` |
| Java / Kotlin (Spring Boot, Quarkus, Micronaut) | **Online initializers** (start.spring.io, code.quarkus.io, launch.micronaut.io): the official project zip in ~2–3 s, **no JDK on the server** |
| React + Spring Boot | Spring Initializr into `backend/` + `npx --yes create-vite@latest frontend --template react-ts` |
| C / C++ | `uvx conan new cmake_exe -d name=<repo> -d version=0.1` (runs through `uv`, nothing to install) |
| Flutter / Dart | `flutter create --org com.example --project-name <name> .`, `dart create -t console <name>` |
| Go / Rust | `go mod init <module>` + `go get <deps>`, `cargo init --name <name> --vcs none` + `cargo add <deps>` |
| PHP (Laravel, Symfony, Slim…) | `composer create-project laravel/laravel .` (no install, no scripts) |
| Elixir / Phoenix | `mix new <name>`, `mix phx.new <name> --no-install` |
| Java / Kotlin via CLI | `mvn archetype:generate ...`, `gradle init --type java-application ...` (the online initializers are faster) |
| Deno, Bun, Pulumi | `deno init`, `bun init --yes`, `pulumi new typescript --generate-only` |
| Ruby (Rails), Swift | In the catalog, but not installed here (see *Scaffolding toolchains*): the agent offers the closest alternatives |

All generators are described in one place, `backend/app/services/scaffold_catalog.py`: the allowed commands, the headless
and skip-install flags added automatically, the files each generator must produce, and the reference command the agent
sees. The agent only gets the generators that can actually run here. When the stack you chose can't be generated (its
toolchain isn't installed), the agent doesn't switch stacks on its own: it lists options (the closest stacks that can be
generated, or installing the toolchain) and waits for you. Generators always run at `@latest`, and the version actually
used (e.g. `create-vite 9.2.1`, `Spring Boot 4.1.1`) is shown and written into the generator commit message.

`backend/app/services/scaffold_service.py` runs them in an empty temporary folder (the repository root) and reads back
every file byte for byte: empty files stay empty, images are kept as binary (pushed base64-encoded), and installer
folders (`node_modules`, `.venv`, `.git`, `obj`...) are skipped. No file content is written by DevFoundry itself; a single
application is generated at the repository root, several parts get one folder each.

Safety: no shell is used; only allow-listed executables and subcommands (`npx`, `npm create/init/install/pkg`, `pnpm`, `yarn`,
`uv init/add/remove/lock`, `uvx`, `dotnet new/sln/add`, `go mod/get`, `cargo new/init/add`, `flutter create`, `dart create`,
`composer create-project/require`, `mix new/phx.new`, `mvn archetype:generate`, `gradle init`, `deno init/add`,
`bun init/create/add`, `pulumi new`; full list in `scaffold_catalog.ALLOWED_COMMANDS`) and
allow-listed generator packages run; arguments cannot contain shell operators, URLs, absolute paths or `..`; `npm install`
never runs dependency scripts; the commands get a minimal environment **without** the PAT, Supabase or OpenAI keys, and a
timeout. Generators still download and run their npm/PyPI package on the backend host, so in a deployment run the backend
in a container or sandbox.

**Routing:** the Orchestrator returns a structured routing decision through Agent Framework's `response_format`. Code checks the decision against the operations allowed in the current stage (`backend/app/services/workflow_routing.py`).
- **No skipped steps:** approving the PRD or TRD only confirms it. Moving on to the TRD or to repository setup needs an explicit request.
- **Technology requests:** any message about technology updates the TRD, even if the TRD already uses that technology.

**Changes at any stage:** the PRD and TRD can be updated at any time, even after the project, repository or push exist.
- **Version tracking:** each document has a version number, and the TRD, project and structure record which version they were built from.
- **Out-of-date items:** a PRD change marks the TRD as out of date. A TRD change marks the project proposal (or the created ADO project) and the repository structure as out of date.
- **Updating them:** the agent offers to update each out-of-date item. Created ADO projects can be renamed or re-described. A structure that was already pushed is updated with a follow-up commit that adds, edits and deletes only what changed.
- **Suggestion chips:** every chat reply, and every conversation load, includes `suggestions`, which are next steps worked out from the stored state. The dashboard shows them as clickable chips above the input box.

- **Database:** run these migrations once, in order, in the Supabase SQL editor:
  1. `backend/db/migrations/001_create_project_and_repository.sql` creates the `Project` table (one row per conversation, with the PRD, TRD and ADO project) and the `Repository` table (the structure, its version and the commit).
  2. `backend/db/migrations/002_document_versions.sql` adds the version columns used to detect out-of-date items, plus the baseline used for follow-up commits.
- **Code:**
  - `backend/app/services/workflow_routing.py`: the routing decision, the stage checks and the suggestions.
  - `backend/app/services/project_store.py`: database access and out-of-date detection.
  - `backend/app/services/repo_structure.py`: validation, applying changes and drawing the tree.
  - `backend/app/services/agents/repo_setup_agent.py`: the agent and its tools.
  - `backend/app/services/mcp/ado_git_push.py`: the push, using the Azure DevOps Git *Pushes - Create* API.

### Scaffolding toolchains (no admin rights needed)

The structure can only be generated for stacks whose CLI is installed on the machine that runs the backend.
`node`/`npx`, `uv` and the .NET SDK are the base requirements. Everything else is installed **for the current user** by one
script, without administrator rights, installers or UAC prompts. It uses official portable archives in
`%LOCALAPPDATA%\DevFoundry\toolchains` and adds them to the user PATH:

```powershell
powershell -ExecutionPolicy Bypass -File backend\scripts\install_toolchains.ps1              # all (~5 GB, 15-30 min)
powershell -ExecutionPolicy Bypass -File backend\scripts\install_toolchains.ps1 -Only go,rust # a subset
powershell -ExecutionPolicy Bypass -File backend\scripts\install_toolchains.ps1 -Force        # update to the latest versions
```

It installs Bun, Deno, Go, Rust (GNU toolchain, needs no Visual Studio), Dart, Flutter, Java (Microsoft OpenJDK LTS), Maven,
Gradle, PHP + Composer, Erlang + Elixir (+ Hex and the Phoenix generator) and Pulumi. **Restart the backend afterwards** so it
sees the new PATH. At startup the backend detects the installed tools in the background, and the agent only gets the
generators that can run.

Not installed: **Ruby/Rails**, because `gem install rails` needs the RubyInstaller DevKit (an installer), and **Swift**, which
has no practical Windows toolchain. For these stacks the agent offers the closest stacks that can be generated.

## Environment variables (root `.env`)

| Variable | Required | Purpose |
| --- | --- | --- |
| `AZURE_DEVOPS_ORG_NAME` | yes | Organization name only, for example `contoso` |
| `AZURE_DEVOPS_PAT` | yes | Personal access token with the scopes listed below |
| `ADO_SCAFFOLD_COMMIT_MESSAGE` | no | Default commit message. The default is `chore: initial project scaffold`. |
| `ADO_SCAFFOLD_DEFAULT_BRANCH` | no | Branch to create in the empty repository. The default is `main`. |
| `TECH_CHANGE_CONFIRM_THRESHOLD` / `TECH_ADDITION_MIN_FIT` | no | Technology-change check before every TRD update: a replacement is confirmed with the user when it fits at least `3` points (1-10) worse than the current choice; an addition or removal when its own fit is `4` or lower. |
| `TITLE_CONFIDENCE_THRESHOLD` | no | How sure (0-1) the Orchestrator must be that the chat describes a specific product before it names the chat. The default is `0.7`; below it the chat stays "New Project". After the Azure DevOps project is created, the chat title is always the project name. |
| `SCAFFOLD_COMMAND_TIMEOUT_SECONDS` | no | Time limit per generator command. The default is `600` (first runs of Maven, Gradle, Pulumi and `go get` download their caches). |
| `SCAFFOLD_ALLOWED_PACKAGES` | no | Extra generator packages `npx` / `uvx` / `npm create` may run (comma separated), e.g. `create-my-template` |

The PAT needs **Project and Team (Read, write & manage)** to create projects, **Code (Read & write)** to create repositories and push, and **Work Items (Read & write)** to create the Azure Boards work items.

## Tests

From `backend`, with the venv activated:

```powershell
# Unit tests (mocked ADO and Supabase, no network)
pytest tests/test_ado_git_push.py tests/test_repo_structure.py tests/test_ado_tools.py tests/test_repo_setup_workflow.py tests/test_scaffold_service.py

# Live Azure DevOps tests. They create and delete repos inside a THROWAWAY project, plus one temporary project.
$env:ADO_INTEGRATION_TESTS="1"; $env:ADO_IT_PROJECT="DevFoundry-Scaffold-IT"; pytest -m integration -s
# Add the Supabase round-trip test (needs the migration):
$env:SUPABASE_INTEGRATION_TESTS="1"

# Full flow end to end (real Azure OpenAI, generator commands and Azure DevOps; in-memory database): a CLI app and a
# React + FastAPI app, from the description to the pushed commits. Each creates and deletes a "DevFoundry-E2E-<id>" project.
$env:ADO_E2E_TESTS="1"; pytest tests/test_repo_setup_agent_e2e.py -m e2e -s
```

# Guardrails Layer

DevFoundry implements a defense-in-depth safety layer via the `openai-guardrails` Python package, executed exclusively against the local SLM through an OpenAI-compatible endpoint.

- **Centralized Layer (`backend/app/services/guardrails_layer/`):** All guardrail clients, configs, and check handlers are encapsulated. Agents interact via unified functions without directly importing the guardrails library.
- **2-Boundary Orchestrator Architecture:**
  - **Input Boundary:** Enforces `Off-Topic Prompts` and `Prompt Injection Detection`. Blocks non-software engineering or adversarial attempts immediately with audit logging.
  - **Output Boundary:** Redacts sensitive PII (emails, phone numbers, credit cards, bank accounts, SSNs) using spaCy NLP + Presidio, and applies `URL Filter` / `Restrict URLs` allowing only trusted domains (`dev.azure.com`, `azure.com`, `github.com`, `microsoft.com`, `maqsoftware.net`, etc.).
- **Child Agent Boundaries:** The Requirements Agent enforces input validation against `System Prompt Leakage` and output validation against `Hallucinated Functions / Tool Misuse`, returning structured clarification JSON when triggered.
- **Fail-Closed Security:** Configured with `raise_guardrail_errors=True` so any evaluation error or tripwire safely halts generation and logs the event for security auditing.

```powershell
# Run Guardrails test suite
pytest backend/tests/test_guardrails.py -v
```

# Build and Test

**Backend tests** — from `backend/` with the virtual environment activated:

```powershell
.\.venv\Scripts\Activate.ps1
pytest
```

**Frontend production build** — from `frontend/`:

```powershell
npm run build
```

**Frontend linting** — from `frontend/`:

```powershell
npm run lint
```

