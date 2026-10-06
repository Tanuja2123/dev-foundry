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

# Build and Test

Backend tests, from `backend` with the virtual environment activated:

```powershell
pytest
```

Frontend production build, from `frontend`:

```powershell
npm run build
```
