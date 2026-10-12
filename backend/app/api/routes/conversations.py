from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from typing import Optional

from app.schemas.conversation import ChatResponse, ConversationItem
from app.schemas.project import ProjectOverview
from app.services.conversation_service import (
    get_conversations,
    process_chat_request,
    create_new_conversation,
    delete_conversation,
    get_conversation_by_id,
    get_suggestions,
)
from app.services.projects_service import get_projects_overview
from app.core.logger import logger

router = APIRouter(prefix="/api", tags=["conversation"])


@router.get("/conversation", response_model=list[ConversationItem])
def list_conversations() -> list[ConversationItem]:
    logger.info("[Route] GET /api/conversation called")
    return get_conversations()


@router.get("/conversation/{conversation_id}")
def get_single_conversation(conversation_id: str):
    logger.info(f"[Route] GET /api/conversation/{conversation_id} called")
    conv = get_conversation_by_id(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found",
        )
    return {**conv, "suggestions": get_suggestions(conversation_id)}


@router.post("/conversation", response_model=ConversationItem)
def create_conversation() -> ConversationItem:
    logger.info("[Route] POST /api/conversation called -> Creating new conversation")
    return create_new_conversation()



@router.delete("/conversation/{conversation_id}")
def remove_conversation(conversation_id: str):
    logger.info(f"[Route] DELETE /api/conversation/{conversation_id} called")
    try:
        delete_conversation(conversation_id)
        return {"status": "success", "conversation_id": conversation_id}
    except Exception as exc:
        logger.error(f"[Route] Error deleting conversation: {str(exc)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete conversation: {str(exc)}",
        ) from exc



@router.post("/chat", response_model=ChatResponse)
async def create_chat(
    conversation_id: Optional[str] = Form(None, description="Optional existing conversation identifier. If null/omitted, a new conversation is created automatically."),
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


@router.get("/projects", response_model=list[ProjectOverview])
def list_projects() -> list[ProjectOverview]:
    logger.info("[Route] GET /api/projects called")
    return get_projects_overview()
