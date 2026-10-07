from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from typing import Optional

from app.schemas.conversation import ChatResponse, ConversationItem
from app.services.conversation_service import get_conversations, process_chat_request, create_new_conversation
from app.core.logger import logger

router = APIRouter(prefix="/api", tags=["conversation"])


@router.get("/conversation", response_model=list[ConversationItem])
def list_conversations() -> list[ConversationItem]:
    logger.info("[Route] GET /api/conversation called")
    return get_conversations()


@router.post("/conversation", response_model=ConversationItem)
def create_conversation() -> ConversationItem:
    logger.info("[Route] POST /api/conversation called -> Creating new conversation")
    return create_new_conversation()


@router.post("/chat", response_model=ChatResponse)
async def create_chat(
    conversation_id: str = Form(..., description="Existing conversation identifier"),
    user_query: str = Form(..., description="User message for the AI agent"),
    file: Optional[UploadFile] = File(None, description="Optional PDF or Word file")
) -> ChatResponse:
    file_info = f" with file '{file.filename}'" if file else ""
    logger.info(f"[Route] POST /api/chat received for conversation '{conversation_id}'{file_info}")
    logger.info(f"[User Query]: \"{user_query[:100]}{'...' if len(user_query) > 100 else ''}\"")
    try:
        return await process_chat_request(conversation_id, user_query, file)
    except ValueError as exc:
        logger.warning(f"[Route] Conversation not found or invalid input: {str(exc)}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(f"[Route] Error processing chat: {str(exc)}")
        raise
