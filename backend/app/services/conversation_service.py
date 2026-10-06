import os
import uuid
import asyncio
from typing import Dict, List, Optional
from fastapi import UploadFile
from dotenv import load_dotenv

from agent_framework import Agent, AgentExecutor, Message
from agent_framework.openai import OpenAIChatCompletionClient
from supabase import create_client, Client

from app.schemas.conversation import ChatResponse, ConversationItem
from app.services.agents.requirements_agent import process_uploaded_document, get_requirements_agent

# Load variables from .env file
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv() # also try default location

def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("Supabase credentials not found in environment.")
    return create_client(url, key)

def get_conversations() -> List[ConversationItem]:
    supabase = get_supabase_client()
    response = supabase.table("Conversation").select("*").execute()
    
    items = []
    for row in response.data:
        conv_id = row.get("conv_id")
        title = row.get("title", "New Project")
        if conv_id:
            items.append(ConversationItem(conversation_id=conv_id, title=title))
    return items

def create_new_conversation(title: str = "New Project") -> ConversationItem:
    supabase = get_supabase_client()
    new_id = str(uuid.uuid4())
    
    # We include title assuming you added it as suggested. 
    # user_id is hardcoded for now until authentication is added.
    data = {
        "conv_id": new_id,
        "user_id": "default_user", 
        "content": [],
        "title": title
    }
    supabase.table("Conversation").insert(data).execute()
    
    return ConversationItem(conversation_id=new_id, title=title)

def get_conversation_by_id(conversation_id: str) -> Optional[Dict]:
    supabase = get_supabase_client()
    response = supabase.table("Conversation").select("*").eq("conv_id", conversation_id).execute()
    if response.data:
        return response.data[0]
    return None


# Agent Framework Client configuration

def get_agent_framework_client() -> OpenAIChatCompletionClient:
    api_key = os.getenv("AZURE_OPENAI_APIKEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_VERSION", "2024-12-01-preview")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
    
    if not api_key or not endpoint:
        raise ValueError("Azure OpenAI credentials not found in environment.")
        
    return OpenAIChatCompletionClient(
        model=deployment,
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
    )

async def generate_chat_response(user_query: str, history: List[Dict]) -> str:
    """Uses Microsoft Agent Framework to generate a response with conversation history."""
    try:
        client = get_agent_framework_client()

        main_agent_prompt = """
You are DevFoundry, an AI-powered software development orchestration assistant.

Your purpose is to help users plan, configure, and execute software development workflows through specialized child agents.

Your primary responsibilities are:
1. Understand the user's intent.
2. Identify the appropriate DevFoundry workflow.
3. Delegate specialized tasks to the appropriate child agent.
4. Maintain awareness of the current conversation and workflow state.
5. Never execute a downstream workflow when its required information is incomplete.
6. Present the results of child agents clearly to the user.

Available capabilities include:
- Requirement Gathering (handled by Requirements_Agent)
- Repository Setup
- Project Setup
- Pull Request Creation
- Development Planning

When a user starts a new conversation:
1. You MUST prefix your very first response with "TITLE: <a short 3-5 word title describing the user's project>\n\n". Do not include this prefix in subsequent messages.
2. Introduce yourself briefly. Example: "TITLE: Social Media App\n\nHi! I'm DevFoundry..."
3. Do not use a generic greeting such as: "How can I help you today?"
4. After the introduction, present the available actions to the user in a clear list.

When the user selects or requests a workflow:
1. Determine which child agent owns the workflow.
2. Pass the relevant user input and conversation context to that child agent.
3. Wait for the child agent's result.
4. If the child agent reports that required information is missing, present the requested clarification to the user.
5. Do not proceed to the next workflow until the required information has been collected.
6. Once the child agent reports completion, continue the workflow.

You must not invent missing requirements, configuration values, repository information, credentials, or project details. Always rely on explicit user-provided information or information extracted from uploaded files.

The Requirement Agent is responsible for requirement extraction and clarification.
The Main Agent is responsible for orchestration and workflow control.

To route tasks to the Requirement Agent, conclude your response by explicitly invoking the Requirements_Agent to handle the requirement gathering.
"""

        orchestrator = Agent(
            name="Orchestrator",
            instructions=main_agent_prompt,
            client=client,
        )

        requirements_agent = get_requirements_agent(client)

        # Simplified Agent Framework Orchestration:
        # Convert Supabase history into Agent Framework Messages
        message_list = []
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            message_list.append(Message(role=role, contents=[text]))
            
        # Append the new user message
        new_msg = Message(role="user", contents=[user_query])
        message_list.append(new_msg)
        
        # Execute the orchestrator
        orchestrator_response = await orchestrator.run(messages=message_list)
        response_text = orchestrator_response.messages[-1].text if orchestrator_response.messages else "No response."

        title_prefix = ""
        if response_text.startswith("TITLE:"):
            parts = response_text.split("\n\n", 1)
            if len(parts) > 1:
                title_prefix = parts[0] + "\n\n"
                response_text = parts[1]

        # If the orchestrator decides to route to the requirements agent
        if "Requirements_Agent" in response_text:
            req_response = await requirements_agent.run(messages=message_list)
            response_text = req_response.messages[-1].text if req_response.messages else response_text
            
        return title_prefix + response_text

    except Exception as e:
        return f"Error communicating with Agent Framework: {str(e)}"

async def process_chat_request(conversation_id: str, user_query: str, file: Optional[UploadFile] = None) -> ChatResponse:
    conv = get_conversation_by_id(conversation_id)
    if not conv:
        raise ValueError(f"Conversation '{conversation_id}' not found.")

    content_array = conv.get("content") or []
    
    # Process file before generating response so it saves to DB
    print(f"DEBUG FILE UPLOAD: {file}")
    if file:
        print(f"DEBUG FILE NAME: {file.filename}")
        file_info = process_uploaded_document(file)
        print(f"DEBUG FILE INFO LENGTH: {len(file_info)}")
        if file_info:
            user_query = user_query + f"\n\n[Attached File Content]:\n{file_info}"

    response_text = await generate_chat_response(user_query, content_array)
    
    # Extract dynamically generated title on first message
    if response_text.startswith("TITLE:"):
        parts = response_text.split("\n\n", 1)
        if len(parts) > 1:
            new_title = parts[0].replace("TITLE:", "").strip()
            response_text = parts[1].strip()
            supabase = get_supabase_client()
            supabase.table("Conversation").update({"title": new_title}).eq("conv_id", conversation_id).execute()

    content_array.append({"role": "user", "text": user_query})
    content_array.append({"role": "assistant", "text": response_text})
    supabase = get_supabase_client()
    supabase.table("Conversation").update({"content": content_array}).eq("conv_id", conversation_id).execute()

    return ChatResponse(conversation_id=conversation_id, response=response_text)
