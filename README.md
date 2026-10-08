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
| `SUPABASE_URL` | Supabase project URL | Optional — falls back to in-memory store |
| `SUPABASE_KEY` | Supabase service role key | Optional — falls back to in-memory store |
| `AZURE_OPENAI_APIKEY` | Azure OpenAI API key | Optional — falls back to demo responses |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint URL | Optional — falls back to demo responses |
| `AZURE_OPENAI_VERSION` | API version (default: `2024-12-01-preview`) | Optional |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (default: `gpt-4`) | Optional |
| `AZURE_DEVOPS_ORG_NAME` | Azure DevOps organization name or URL | Yes (for ADO project & repo creation) |
| `AZURE_DEVOPS_PAT` | Azure DevOps Personal Access Token (PAT) with project/code read & write scopes | Yes (for ADO project & repo creation) |

> **Without Supabase credentials**, the backend uses an in-memory conversation store (resets on restart).  
> **Without Azure OpenAI credentials**, `/api/chat` returns a placeholder demo response.

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

