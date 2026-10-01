from fastapi import APIRouter, HTTPException, status

from app.schemas.conversation import ChatRequest, ChatResponse, ConversationItem
from app.services.conversation_service import get_conversations, process_chat_request

router = APIRouter(prefix="/api", tags=["conversation"])


@router.get("/conversation", response_model=list[ConversationItem])
def list_conversations() -> list[ConversationItem]:
    return get_conversations()


@router.post("/chat", response_model=ChatResponse)
def create_chat(request: ChatRequest) -> ChatResponse:
    try:
        return process_chat_request(request)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
