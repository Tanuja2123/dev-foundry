from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.conversations import router as conversation_router

app = FastAPI(
    title="DevFoundry Backend",
    version="0.1.0",
    description="Backend APIs for conversation history and chat orchestration.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(conversation_router)


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}
