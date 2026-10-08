from typing import Optional
from pydantic import BaseModel, Field


class ConversationItem(BaseModel):
    conversation_id: str = Field(..., description="Unique identifier for the conversation")
    title: str = Field(..., description="Short title shown in the UI")


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = Field(None, description="Optional existing conversation identifier")
    user_query: str = Field(..., min_length=1, description="User message for the AI agent")



class ChatResponse(BaseModel):
    conversation_id: str = Field(..., description="Conversation identifier for the response")
    response: str = Field(..., description="LLM-generated answer for the user query")
