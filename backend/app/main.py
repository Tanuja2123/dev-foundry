import os
import threading
import time
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Load root .env and local .env into environment
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env"))
load_dotenv()

from app.api.routes.conversations import router as conversation_router  # noqa: E402
from app.core.logger import logger  # noqa: E402
from app.services.scaffold_service import available_toolchains  # noqa: E402


def _detect_toolchains() -> None:
    try:
        logger.info(f"[Scaffold] Toolchains available: {', '.join(available_toolchains()) or 'none'}")
    except Exception as exc:  # detection is retried on the first scaffold request
        logger.warning(f"[Scaffold] Toolchain detection failed: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Detect the installed scaffold toolchains in the background: JVM tools and Flutter take up to ~20 s to report
    # their version, which would otherwise delay the first repository-structure request.
    threading.Thread(target=_detect_toolchains, name="toolchain-warmup", daemon=True).start()
    yield


app = FastAPI(
    title="DevFoundry Backend",
    version="0.1.0",
    description="Backend APIs for conversation history and chat orchestration.",
    lifespan=lifespan,
)

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/chat/progress/"):       # polled every 2 s while a chat runs: not logged
        return await call_next(request)
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
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(conversation_router)


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


