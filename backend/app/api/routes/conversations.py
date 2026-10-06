from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from typing import Optional

from app.schemas.conversation import ChatResponse, ConversationItem
from app.services.conversation_service import get_conversations, process_chat_request, create_new_conversation

router = APIRouter(prefix="/api", tags=["conversation"])


@router.get("/conversation", response_model=list[ConversationItem])
def list_conversations() -> list[ConversationItem]:
    return get_conversations()


@router.post("/conversation", response_model=ConversationItem)
def create_conversation() -> ConversationItem:
    return create_new_conversation()


@router.post("/chat", response_model=ChatResponse)
async def create_chat(
    conversation_id: str = Form(..., description="Existing conversation identifier"),
    user_query: str = Form(..., description="User message for the AI agent"),
    file: Optional[UploadFile] = File(None, description="Optional PDF or Word file")
) -> ChatResponse:
    try:
        return await process_chat_request(conversation_id, user_query, file)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
