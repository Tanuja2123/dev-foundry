from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from typing import Optional

from app.core.auth import CurrentUser, get_current_user
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

# Every route requires a signed-in user (Microsoft Entra ID token) and only sees that user's conversations.
router = APIRouter(prefix="/api", tags=["conversation"])


def _not_found(conversation_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail=f"Conversation '{conversation_id}' not found")


@router.get("/conversation", response_model=list[ConversationItem])
def list_conversations(user: CurrentUser = Depends(get_current_user)) -> list[ConversationItem]:
    logger.info(f"[Route] GET /api/conversation called by user '{user.user_id}'")
    return get_conversations(user.user_id)


@router.get("/conversation/{conversation_id}")
def get_single_conversation(conversation_id: str, user: CurrentUser = Depends(get_current_user)):
    logger.info(f"[Route] GET /api/conversation/{conversation_id} called")
    conv = get_conversation_by_id(conversation_id, user.user_id)
    if not conv:
        raise _not_found(conversation_id)
    return {**conv, "suggestions": get_suggestions(conversation_id)}


@router.post("/conversation", response_model=ConversationItem)
def create_conversation(user: CurrentUser = Depends(get_current_user)) -> ConversationItem:
    logger.info("[Route] POST /api/conversation called -> Creating new conversation")
    return create_new_conversation(user.user_id)



@router.delete("/conversation/{conversation_id}")
def remove_conversation(conversation_id: str, user: CurrentUser = Depends(get_current_user)):
    logger.info(f"[Route] DELETE /api/conversation/{conversation_id} called")
    try:
        deleted = delete_conversation(conversation_id, user.user_id)
    except Exception as exc:
        logger.error(f"[Route] Error deleting conversation: {str(exc)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete conversation: {str(exc)}",
        ) from exc
    if not deleted:
        raise _not_found(conversation_id)
    return {"status": "success", "conversation_id": conversation_id}



@router.post("/chat", response_model=ChatResponse)
async def create_chat(
    conversation_id: Optional[str] = Form(None, description="Optional existing conversation identifier. If null/omitted, a new conversation is created automatically."),
    user_query: str = Form(..., description="User message for the AI agent"),
    file: Optional[UploadFile] = File(None, description="Optional PDF or Word file"),
    user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    file_info = f" with file '{file.filename}'" if file else ""
    logger.info(f"[Route] POST /api/chat received for conversation '{conversation_id}'{file_info}")
    logger.info(f"[User Query]: \"{user_query[:100]}{'...' if len(user_query) > 100 else ''}\"")
    try:
        return await process_chat_request(conversation_id, user_query, file, user_id=user.user_id)
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
