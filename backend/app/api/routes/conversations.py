from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel
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
from app.services import progress
from app.services.projects_service import get_projects_overview, update_project_name
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
        logger.exception(f"[Route] Error deleting conversation '{conversation_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The conversation could not be deleted. Please try again.",
        ) from exc
    if not deleted:
        raise _not_found(conversation_id)
    return {"status": "success", "conversation_id": conversation_id}



@router.get("/chat/progress/{conversation_id}")
def chat_progress(conversation_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
    """What a running chat turn is doing right now (polled by the dashboard while it waits)."""
    current = progress.get(conversation_id, user.user_id)
    return {"running": current is not None, **(current or {})}


@router.post("/chat/cancel/{conversation_id}")
def cancel_chat(conversation_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
    """
    Stop: cancels the conversation's running turn unless an Azure DevOps action has already started.
    {"status": "cancelled"} | {"status": "finishing", "action": "..."} | {"status": "idle"}
    """
    result = progress.cancel(conversation_id, user.user_id)
    logger.info(f"[Route] Stop requested for conversation '{conversation_id}': {result.get('status')}"
                + (f" ({result['action']} already started)" if result.get("action") else ""))
    return result


@router.post("/chat", response_model=ChatResponse)
async def create_chat(
    conversation_id: Optional[str] = Form(None, description="Optional existing conversation identifier. If null/omitted, a new conversation is created automatically."),
    user_query: str = Form(..., description="User message for the AI agent"),
    file: Optional[UploadFile] = File(None, description="Optional PDF or Word file"),
    user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    clean_conv_id = None if (not conversation_id or str(conversation_id).strip().lower() in ("null", "none", "")) else conversation_id.strip()
    file_info = f" with file '{file.filename}'" if file else ""
    logger.info(f"[Route] POST /api/chat received for conversation '{clean_conv_id}'{file_info}")
    logger.info(f"[User Query]: \"{user_query[:100]}{'...' if len(user_query) > 100 else ''}\"")
    try:
        return await process_chat_request(clean_conv_id, user_query, file, user_id=user.user_id)
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
def list_projects(user: CurrentUser = Depends(get_current_user)) -> list[ProjectOverview]:
    logger.info(f"[Route] GET /api/projects called by user '{user.user_id}'")
    return get_projects_overview(user.user_id)


class RenameProjectRequest(BaseModel):
    project_name: str


@router.patch("/projects/{conversation_id}", response_model=ProjectOverview)
def rename_project(
    conversation_id: str,
    payload: RenameProjectRequest,
    user: CurrentUser = Depends(get_current_user),
) -> ProjectOverview:
    logger.info(f"[Route] PATCH /api/projects/{conversation_id} called by user '{user.user_id}' with name '{payload.project_name}'")
    try:
        return update_project_name(conversation_id, payload.project_name, user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"[Route] Error renaming project: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
