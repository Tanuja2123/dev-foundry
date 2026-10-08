# DevFoundry

Requirement-to-delivery agent. The frontend is a React app with Microsoft sign-in. The backend is a FastAPI app that serves conversation and chat endpoints.

# Getting Started

Run the backend and the frontend in two terminals. The frontend expects the API at `http://localhost:8000`.

## Backend

From the repo root, in PowerShell:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirement.txt
uvicorn app.main:app --reload --port 8000
```

The API is at `http://localhost:8000`. Interactive docs are at `http://localhost:8000/docs`.

## Frontend

Node.js is required. From the repo root, in a second terminal:

```powershell
cd frontend
copy .env.example .env
npm install
npm run dev
```

Open the URL Vite prints, usually `http://localhost:5173`.

Before `npm run dev`, fill in `frontend/.env`:

- `VITE_AZURE_CLIENT_ID` — Entra app registration client ID
- `VITE_AZURE_TENANT_ID` — Entra tenant ID
- `VITE_AZURE_REDIRECT_URI` — `http://localhost:5173/auth-callback`
- `VITE_API_BASE_URL` — `http://localhost:8000`

Register `http://localhost:5173/auth-callback` as a Single-page application redirect URI on that app registration. Restart `npm run dev` after changing `.env`.

# API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Returns `{ "status": "ok" }` |
| GET | `/api/conversation` | Lists demo conversations (`conversation_id`, `title`) |
| POST | `/api/chat` | Sends `{ "conversation_id", "user_query" }` and returns `{ "conversation_id", "response" }` |

`POST /api/chat` returns 404 when `conversation_id` is not one of the demo conversations. Chat replies are a placeholder, not a live model.

# Azure DevOps Repository Setup

After the PRD and TRD are approved, the Repo Setup Agent works through these steps. Each step waits for the user's explicit approval.

1. **Project proposal:** the agent proposes an ADO project name and description from the PRD and TRD. The user can change them as often as needed.
2. **Create project:** the agent creates the ADO project, waits until ADO has finished provisioning it, and saves the real project id. It then suggests a repository name.
3. **Repository name:** the user can change the suggested name. The TRD isn't re-opened at this point.
4. **Create repository:** the agent creates an empty repository. It then designs a folder structure from the TRD, for any tech stack, and shows it.
5. **Structure review:** the user can request changes. Each change set produces a new structure version.
6. **Push:** the agent pushes the approved structure to `main` as one commit.

The structure shown in chat and the files pushed are guaranteed to be the same. Both come from the `Repository.structure` row in Supabase: the tree is drawn by code, and the push tool reads that row instead of taking files from the model.

- **Database:** run `backend/db/migrations/001_create_project_and_repository.sql` once in the Supabase SQL editor. It creates the `Project` table (one row per conversation, with the PRD, TRD and ADO project) and the `Repository` table (the structure, its version and the commit).
- **Code:**
  - `backend/app/services/project_store.py`: database access.
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

Backend tests, from `backend` with the virtual environment activated:

```powershell
pytest
```

Frontend production build, from `frontend`:

```powershell
npm run build
```
