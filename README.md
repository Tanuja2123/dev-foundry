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

