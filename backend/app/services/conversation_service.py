import json
import os
import time
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
from app.core.logger import logger

# Load variables from .env file
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv() # also try default location

def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        logger.error("[Supabase] Credentials missing in environment (.env).")
        raise ValueError("Supabase credentials not found in environment.")
    return create_client(url, key)

def get_conversations() -> List[ConversationItem]:
    supabase = get_supabase_client()
    try:
        response = supabase.table("Conversation").select("*").order("created_at", desc=True).execute()
        items = []
        for row in response.data:
            conv_id = row.get("conv_id")
            title = row.get("title", "New Project")
            if conv_id:
                items.append(ConversationItem(conversation_id=conv_id, title=title))
        logger.info(f"[Supabase] Retrieved {len(items)} conversations from DB.")
        return items
    except Exception as e:
        logger.error(f"[Supabase] Error fetching conversations: {str(e)}")
        raise

def create_new_conversation(title: str = "New Project") -> ConversationItem:
    supabase = get_supabase_client()
    new_id = str(uuid.uuid4())
    data = {
        "conv_id": new_id,
        "user_id": "default_user", 
        "content": [],
        "title": title
    }
    try:
        supabase.table("Conversation").insert(data).execute()
        logger.info(f"[Supabase] Created new conversation '{new_id}' (Title: '{title}')")
        return ConversationItem(conversation_id=new_id, title=title)
    except Exception as e:
        logger.error(f"[Supabase] Failed to create new conversation: {str(e)}")
        raise

def delete_conversation(conversation_id: str) -> bool:
    try:
        uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return False

    supabase = get_supabase_client()
    try:
        supabase.table("Conversation").delete().eq("conv_id", conversation_id).execute()
        logger.info(f"[Supabase] Deleted conversation '{conversation_id}'")
        return True
    except Exception as e:
        logger.error(f"[Supabase] Failed to delete conversation '{conversation_id}': {str(e)}")
        raise


def get_conversation_by_id(conversation_id: str) -> Optional[Dict]:
    try:
        uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return None

    supabase = get_supabase_client()
    try:
        response = supabase.table("Conversation").select("*").eq("conv_id", conversation_id).execute()
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        logger.error(f"[Supabase] Error retrieving conversation '{conversation_id}': {str(e)}")
        raise



# Agent Framework Client configuration

def get_agent_framework_client() -> OpenAIChatCompletionClient:
    api_key = os.getenv("AZURE_OPENAI_APIKEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_VERSION", "2024-12-01-preview")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
    
    if not api_key or not endpoint:
        logger.error("[Azure OpenAI] Credentials missing in environment.")
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
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")

        main_agent_prompt = """
You are DevFoundry, an AI-powered software development orchestration assistant.

Your purpose is to understand the user's intent within the full conversation context, track workflow state across the software delivery lifecycle, and dynamically coordinate with the specialized Requirements_Agent.

AVAILABLE CHILD AGENT:
- Requirements_Agent:
  Responsible for both:
  1. Product Requirements (PRD): Extracts and structures Objective, Problem Statement, Goals, User Experience, User Flow. Clarifies ambiguities and updates PRD based on user feedback.
  2. Technical Architecture (TRD): Generates and updates the Technical Requirements Document (Frontend, Backend, Database, Infrastructure, Security) based on the approved PRD and user tech stack feedback.

DYNAMIC CONTEXTUAL WORKFLOW ROUTING PRINCIPLES:
- Do NOT rely on isolated keywords. Evaluate the user's message IN THE CONTEXT of the previous conversation history and current workflow stage.
- If the assistant previously offered: "Would you like me to create a Technical Requirements Document (TRD) as well?" and the user confirms, responds affirmatively (e.g. "yes", "sure", "go ahead"), or asks to proceed with technical architecture:
  Recognize that the workflow stage is TRD and delegate to Requirements_Agent with operation: "GENERATE_TRD".
- If a TRD was already generated and the user requests tech stack modifications (e.g. "Use Python FastAPI instead of Node", "Use PostgreSQL instead of MongoDB"):
  Recognize that the workflow stage is TRD modification and delegate to Requirements_Agent with operation: "UPDATE_TRD".
- If the user provides a project description, uploaded requirement document, answers PRD clarification questions, or requests changes to the functional PRD:
  Recognize that the workflow stage is PRD and delegate to Requirements_Agent with operation: "GENERATE_PRD" or "UPDATE_PRD".
- If the user query is generic, conversational, informational, or does not require requirements/technical delivery functionality, answer directly and do NOT invoke the child agent.

When a user starts a new conversation:
- You MUST prefix your very first response with "TITLE: <a short 3-5 word title describing the user's project>\\n\\n". Do not include this prefix in subsequent messages.

DELEGATION INSTRUCTION:
- If Requirements_Agent is required, output:
  "DELEGATE_TO: Requirements_Agent | WORKFLOW: <PRD or TRD> | OPERATION: <GENERATE_PRD, UPDATE_PRD, GENERATE_TRD, or UPDATE_TRD>"
- If no child agent is required, provide your direct, helpful response to the user.
"""

        orchestrator = Agent(
            name="Orchestrator",
            instructions=main_agent_prompt,
            client=client,
        )

        requirements_agent = get_requirements_agent(client)

        # Convert history into Agent Framework Messages
        message_list = []
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            message_list.append(Message(role=role, contents=[text]))
            
        new_msg = Message(role="user", contents=[user_query])
        message_list.append(new_msg)

        # 1. Execute the Orchestrator
        logger.info(f"[Orchestrator] Running Orchestrator Agent (Model: {deployment}, History: {len(history)} messages)...")
        orch_start = time.time()
        orchestrator_response = await orchestrator.run(messages=message_list)
        orch_duration = round((time.time() - orch_start) * 1000, 1)
        response_text = orchestrator_response.messages[-1].text if orchestrator_response.messages else "No response."
        logger.info(f"[Orchestrator] Completed in {orch_duration}ms")

        title_prefix = ""
        if response_text.startswith("TITLE:"):
            parts = response_text.split("\n\n", 1)
            if len(parts) > 1:
                title_prefix = parts[0] + "\n\n"
                response_text = parts[1]

        # 2. Dynamic Workflow Routing Evaluation
        should_delegate = "Requirements_Agent" in response_text or "DELEGATE_TO: Requirements_Agent" in response_text

        if should_delegate:
            logger.info(f"[Routing Decision] -> Delegating to Requirements_Agent")

            # Extract workflow context from Orchestrator's decision if present
            workflow_context_note = ""
            if "OPERATION:" in response_text:
                op_part = response_text.split("OPERATION:", 1)[1].strip()
                workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {op_part}]"
            elif "TRD" in response_text:
                workflow_context_note = "\n[Workflow Context from Orchestrator: Workflow = TRD]"
            else:
                workflow_context_note = "\n[Workflow Context from Orchestrator: Workflow = PRD]"

            # Prepare message list for Requirements_Agent with clear workflow context
            req_messages = list(message_list[:-1])
            # Inject workflow context instruction into the user's turn
            context_user_msg = Message(role="user", contents=[user_query + workflow_context_note])
            req_messages.append(context_user_msg)

            # Invoke Requirements Child Agent
            logger.info(f"[Requirements_Agent] Processing request with workflow context: {workflow_context_note.strip()}...")
            child_start = time.time()
            child_response = await requirements_agent.run(messages=req_messages)
            child_duration = round((time.time() - child_start) * 1000, 1)
            child_raw_json = child_response.messages[-1].text if child_response.messages else ""
            logger.info(f"[Requirements_Agent] Completed in {child_duration}ms")

            # 3. Follow-Up Flow: Child Agent -> Orchestrator Synthesizer -> User
            logger.info("[Orchestrator] Converting Requirements_Agent output into user-facing natural language...")
            synthesis_prompt = f"""
You are the DevFoundry Orchestrator. 
The Requirements_Agent has produced the following structured output:
{child_raw_json}

Your task:
Convert this output into a friendly, structured, and professional response for the user:

CASE 1: If status is "CLARIFICATION_REQUIRED" or "NEEDS_INPUT":
- Briefly acknowledge the information gathered so far.
- Ask the clarifying follow-up question(s) specified in "questions" or "next_question".
- If "choices" are provided, present them clearly as selectable options/bullet points.

CASE 2: If status is "COMPLETE" and workflow_stage is "PRD" (or contains structured_project_response):
- Present the complete structured PRD specification using clear Markdown headings:
  * **Objective**
  * **Problem Statement**
  * **Goals** (bulleted list)
  * **User Experience**
  * **User Flow** (step-by-step numbered list)
- Acknowledge that the functional requirements are complete.
- Naturally offer the next logical workflow step by concluding with:
  "**Would you like me to create a Technical Requirements Document (TRD) as well?**"

CASE 3: If status is "COMPLETE" and workflow_stage is "TRD" (or contains technical_requirements_document):
- Present the comprehensive technical stack recommendations using clean Markdown headings:
  * **Frontend Technology Stack** (Framework, Supporting Technologies, Tools & Libraries, Rationale)
  * **Backend Technology Stack** (Language, Framework, API Technologies, Supporting Technologies, Rationale)
  * **Database Technology Stack** (Database, Database Type, Supporting Technologies, Rationale)
  * **Integration & Infrastructure** (if present)
- If changes were made to an existing architecture, highlight what was updated based on change_notes.
- Conclude by asking if the user would like to make any modifications to these technology choices, or if they are ready to proceed with repository setup.

Do NOT output raw JSON. Output only clean, beautifully formatted Markdown text.
"""
            synth_start = time.time()
            synthesizer_agent = Agent(
                name="Orchestrator_Synthesizer",
                instructions=synthesis_prompt,
                client=client,
            )
            synthesis_result = await synthesizer_agent.run(messages=[Message(role="user", contents=["Please present this update to the user."])])
            synth_duration = round((time.time() - synth_start) * 1000, 1)
            response_text = synthesis_result.messages[-1].text if synthesis_result.messages else child_raw_json
            logger.info(f"[Orchestrator] Response synthesized ({synth_duration}ms)")
        else:
            logger.info("[Routing Decision] -> Handled directly by Orchestrator (Path A Generic/Conversational)")

        return title_prefix + response_text

    except Exception as e:
        logger.error(f"[Agent Pipeline] Error: {str(e)}")
        return f"Error communicating with Agent Framework: {str(e)}"

async def process_chat_request(conversation_id: str, user_query: str, file: Optional[UploadFile] = None) -> ChatResponse:
    overall_start = time.time()
    logger.info(f"=== Starting chat processing pipeline for conversation '{conversation_id}' ===")

    conv = get_conversation_by_id(conversation_id)
    if not conv:
        logger.warning(f"[Supabase] Conversation '{conversation_id}' not found in DB.")
        raise ValueError(f"Conversation '{conversation_id}' not found.")

    content_array = conv.get("content") or []
    
    # Process document upload if provided
    if file:
        file_info = process_uploaded_document(file)
        if file_info:
            user_query = user_query + f"\n\n[Attached File Content]:\n{file_info}"

    # Generate response via multi-agent pipeline
    response_text = await generate_chat_response(user_query, content_array)
    
    # Extract dynamically generated title on first message
    if response_text.startswith("TITLE:"):
        parts = response_text.split("\n\n", 1)
        if len(parts) > 1:
            new_title = parts[0].replace("TITLE:", "").strip()
            response_text = parts[1].strip()
            supabase = get_supabase_client()
            try:
                supabase.table("Conversation").update({"title": new_title}).eq("conv_id", conversation_id).execute()
                logger.info(f"[Supabase] Updated conversation title to: '{new_title}'")
            except Exception as e:
                logger.error(f"[Supabase] Failed to update title: {str(e)}")
    elif conv.get("title") in ("New Project", "New Conversation"):
        # Fallback: Auto-generate a title from the user query if LLM didn't prefix TITLE:
        clean_query = user_query.split("[Attached File Content]")[0].strip()
        if clean_query:
            first_line = clean_query.split("\n")[0].strip()
            words = [w for w in first_line.split() if w]
            candidate_title = " ".join(words[:5])
            if len(candidate_title) > 36:
                candidate_title = candidate_title[:33] + "..."
            if candidate_title:
                supabase = get_supabase_client()
                try:
                    supabase.table("Conversation").update({"title": candidate_title}).eq("conv_id", conversation_id).execute()
                    logger.info(f"[Supabase] Auto-updated default conversation title to: '{candidate_title}'")
                except Exception as e:
                    logger.error(f"[Supabase] Failed to auto-update title: {str(e)}")

    # Persist updated conversation history
    content_array.append({"role": "user", "text": user_query})
    content_array.append({"role": "assistant", "text": response_text})
    supabase = get_supabase_client()
    try:
        supabase.table("Conversation").update({"content": content_array}).eq("conv_id", conversation_id).execute()
        logger.info(f"[Supabase] Saved updated conversation history ({len(content_array)} messages total).")
    except Exception as e:
        logger.error(f"[Supabase] Failed to save conversation history: {str(e)}")
        raise

    total_time = round((time.time() - overall_start) * 1000, 1)
    logger.info(f"=== Chat pipeline completed successfully in {total_time}ms ===")

    return ChatResponse(conversation_id=conversation_id, response=response_text)


