from typing import Dict, List, Optional

from app.schemas.conversation import ChatRequest, ChatResponse, ConversationItem


DEMO_CONVERSATIONS: Dict[str, Dict[str, str]] = {
    "conv_001": {"conversation_id": "conv_001", "title": "Requirements ingestion"},
    "conv_002": {"conversation_id": "conv_002", "title": "Project planning"},
    "conv_003": {"conversation_id": "conv_003", "title": "Sprint backlog generation"},
}


def get_conversations() -> List[ConversationItem]:
    return [ConversationItem(**conversation) for conversation in DEMO_CONVERSATIONS.values()]


def get_conversation_by_id(conversation_id: str) -> Optional[Dict[str, str]]:
    return DEMO_CONVERSATIONS.get(conversation_id)


def generate_chat_response(user_query: str) -> str:
    """Placeholder for OpenAI-style LLM integration.

    In the future, this will send a message array like:
    [{"role": "user", "content": user_query}]
    to an LLM service and return the generated answer.
    """
    return (
        "Demo AI response: I have received your request and am ready to help "
        f"with '{user_query}'. This is a placeholder for the OpenAI-style LLM integration."
    )


def process_chat_request(request: ChatRequest) -> ChatResponse:
    if not get_conversation_by_id(request.conversation_id):
        raise ValueError(f"Conversation '{request.conversation_id}' not found.")

    response_text = generate_chat_response(request.user_query)
    return ChatResponse(conversation_id=request.conversation_id, response=response_text)
