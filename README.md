# DevFoundry

Requirement-to-delivery agent. The frontend is a React (Vite + TypeScript) app with Microsoft Entra sign-in. The backend is a FastAPI app that serves conversation and chat endpoints, backed by Supabase and an Azure OpenAI multi-agent pipeline.

# Prerequisites

| Tool | Version |
| --- | --- |
| Python | 3.12+ |
| Node.js | 18+ |
| npm | 9+ |

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
| `AZURE_DEVOPS_ORG_NAME` | Azure DevOps organization name or URL | Yes (for ADO project & repo creation) |
| `AZURE_DEVOPS_PAT` | Azure DevOps Personal Access Token (PAT) with project/code read & write scopes | Yes (for ADO project & repo creation) |


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
| GET | `/api/conversation` | Lists all conversations (`conversation_id`, `title`) |
| POST | `/api/conversation` | Creates a new conversation; returns `{ "conversation_id", "title" }` |
| POST | `/api/chat` | Accepts JSON `{ "conversation_id", "user_query" }` or multipart form-data with an optional `file` field; returns `{ "conversation_id", "response" }` |

`POST /api/chat` returns 404 when `conversation_id` does not exist.

---

# Sign-in, Ownership and Database Security

- **Sign-in on every API call:** the frontend sends the user's Microsoft Entra ID token (MSAL) as `Authorization: Bearer <token>` on every `/api/*` call.
- **Token validation:** the backend (`backend/app/core/auth.py`) validates it: signature against the tenant's keys, issuer, audience (the app's client id) and expiry. It identifies the user by the immutable Entra object id (`oid`).
- **Ownership:** each conversation belongs to the user who created it. The API lists only your own conversations, and another user's conversation returns **404**. `/health` stays public.
- **User table:** signed-in users are recorded in the `AppUser` table (`user_id` = `oid`, `username`, `display_name`, `email`, `last_seen_at`).
- **Table links:** `Project.conv_id` references `Conversation`, so deleting a conversation deletes its `Project` and `Repository` rows. Nothing is deleted in Azure DevOps.
- **Database access:** the tables are closed to Supabase's anon/authenticated keys. Only the backend, using the **service-role key**, can read and write them.

Database migrations, run in this order in the Supabase SQL editor:
1. `backend/db/migrations/001_create_project_and_repository.sql`
2. `backend/db/migrations/002_document_versions.sql`
3. `backend/db/migrations/003_users_and_ownership.sql`: the `AppUser` table, the Conversation → Project link and the database hardening. **Switch the backend to the service-role key first.**
4. `backend/db/migrations/004_assign_existing_conversations.sql`: assigns the old `default_user` conversations to one account and makes the owner mandatory. Set your email in the file and run it **after signing in once**.

# Azure DevOps Repository Setup

After the PRD and TRD are approved, the Repo Setup Agent works through these steps. Each step waits for the user's explicit approval.

1. **Project proposal:** the agent proposes an ADO project name and description from the PRD and TRD. The user can change them as often as needed.
2. **Create project:** the agent creates the ADO project, waits until ADO has finished provisioning it, and saves the real project id. It then suggests a repository name.
3. **Repository name:** the user can change the suggested name. The TRD isn't re-opened at this point.
4. **Create repository:** the agent creates an empty repository. It then designs a folder structure from the TRD, for any tech stack, and shows it.
5. **Structure review:** the user can request changes. Each change set produces a new structure version.
6. **Push:** the agent pushes the approved structure to `main` as one commit.

The structure shown in chat and the files pushed are guaranteed to be the same. Both come from the `Repository.structure` row in Supabase: the tree is drawn by code, and the push tool reads that row instead of taking files from the model.

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

## Environment variables (root `.env`)

| Variable | Required | Purpose |
| --- | --- | --- |
| `AZURE_DEVOPS_ORG_NAME` | yes | Organization name only, for example `contoso` |
| `AZURE_DEVOPS_PAT` | yes | Personal access token with the scopes listed below |
| `ADO_SCAFFOLD_COMMIT_MESSAGE` | no | Default commit message. The default is `chore: initial project scaffold`. |
| `ADO_SCAFFOLD_DEFAULT_BRANCH` | no | Branch to create in the empty repository. The default is `main`. |

The PAT needs **Project and Team (Read, write & manage)** to create projects and **Code (Read & write)** to create repositories and push.

## Tests

From `backend`, with the venv activated:

```powershell
# Unit tests (mocked ADO and Supabase, no network)
pytest tests/test_ado_git_push.py tests/test_repo_structure.py tests/test_ado_tools.py tests/test_repo_setup_workflow.py

# Live Azure DevOps tests. They create and delete repos inside a THROWAWAY project, plus one temporary project.
$env:ADO_INTEGRATION_TESTS="1"; $env:ADO_IT_PROJECT="DevFoundry-Scaffold-IT"; pytest -m integration -s
# Add the Supabase round-trip test (needs the migration):
$env:SUPABASE_INTEGRATION_TESTS="1"

# Full flow end to end (real Azure OpenAI, Supabase and Azure DevOps). This creates and deletes a "DevFoundry-E2E-<id>" project.
$env:ADO_E2E_TESTS="1"; pytest -m e2e -s
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

