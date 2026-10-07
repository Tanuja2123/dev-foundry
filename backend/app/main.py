import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.conversations import router as conversation_router
from app.core.logger import logger

app = FastAPI(
    title="DevFoundry Backend",
    version="0.1.0",
    description="Backend APIs for conversation history and chat orchestration.",
)

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start_time = time.time()
    logger.info(f"--> [API] {request.method} {request.url.path}")
    response = await call_next(request)
    duration = round((time.time() - start_time) * 1000, 1)
    logger.info(f"<-- [API] {request.method} {request.url.path} finished with status {response.status_code} ({duration}ms)")
    return response

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


