from typing import List, Optional
from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    label: str = Field(..., description="Short chip label shown above the chat input")
    prompt: str = Field(..., description="Message sent when the user clicks the suggestion")


class ConversationItem(BaseModel):
    conversation_id: str = Field(..., description="Unique identifier for the conversation")
    title: str = Field(..., description="Short title shown in the UI")


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = Field(None, description="Optional existing conversation identifier")
    user_query: str = Field(..., min_length=1, description="User message for the AI agent")



class ChatResponse(BaseModel):
    conversation_id: str = Field(..., description="Conversation identifier for the response")
    response: str = Field(..., description="LLM-generated answer for the user query")
    suggestions: List[Suggestion] = Field(default_factory=list, description="Next-step suggestions for the user")
    title: Optional[str] = Field(None, description="Current or updated title for the conversation")
