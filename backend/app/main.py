from fastapi import FastAPI

from app.api.routes.conversations import router as conversation_router

app = FastAPI(
    title="DevFoundry Backend",
    version="0.1.0",
    description="Backend APIs for conversation history and chat orchestration.",
)

app.include_router(conversation_router)


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}
