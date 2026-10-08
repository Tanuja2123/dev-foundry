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
from app.services.agents.repo_setup_agent import get_repo_setup_agent
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

Your purpose is to understand the user's intent within the full conversation context, track workflow state across the software delivery lifecycle, and dynamically coordinate with the specialized child agents.

AVAILABLE CHILD AGENTS:
1. Requirements_Agent:
   Responsible for:
   - Product Requirements (PRD): Extracts and structures Objective, Problem Statement, Goals, User Experience, User Flow. Clarifies ambiguities and updates PRD based on user feedback.
   - Technical Architecture (TRD): Generates and updates the Technical Requirements Document (Frontend, Backend, Database, Infrastructure, Security) based on the approved PRD and user tech stack feedback.

2. Repo_Setup_Agent:
   Responsible for Azure DevOps project and repository setup across a state-driven human-in-the-loop workflow:
   - Proposing project name and description (GENERATE_PROJECT_PROPOSAL)
   - Updating project proposal details (UPDATE_PROJECT_PROPOSAL)
   - Creating the ADO project upon explicit user approval (CREATE_PROJECT)
   - Generating tailored repository directory structure based on PRD/TRD/tech stack (GENERATE_REPOSITORY_STRUCTURE)
   - Updating repository directory structure based on user feedback (UPDATE_REPOSITORY_STRUCTURE)
   - Creating the ADO Git repository upon explicit user approval (CREATE_REPOSITORY)

DYNAMIC CONTEXTUAL WORKFLOW ROUTING PRINCIPLES:
- Do NOT rely on isolated keywords. Evaluate the user's message IN THE CONTEXT of the previous conversation history and current workflow stage.

A. PRD & TRD STAGES (Requirements_Agent):
- If the assistant previously offered: "Would you like me to create a Technical Requirements Document (TRD) as well?" and the user confirms, responds affirmatively (e.g. "yes", "sure", "go ahead"), or asks to proceed with technical architecture:
  Recognize that the workflow stage is TRD and delegate to Requirements_Agent with operation: "GENERATE_TRD".
- If a TRD was already generated and the user requests tech stack modifications (e.g. "Use Python FastAPI instead of Node", "Use PostgreSQL instead of MongoDB"):
  Recognize that the workflow stage is TRD modification and delegate to Requirements_Agent with operation: "UPDATE_TRD".
- If the user provides a project description, uploaded requirement document, answers PRD clarification questions, or requests changes to the functional PRD:
  Recognize that the workflow stage is PRD and delegate to Requirements_Agent with operation: "GENERATE_PRD" or "UPDATE_PRD".

B. ADO PROJECT & REPO SETUP STAGES (Repo_Setup_Agent):
- If TRD is complete and user agrees/asks to proceed with repository setup, create Azure DevOps project, or start repo setup:
  Delegate to Repo_Setup_Agent with operation: "GENERATE_PROJECT_PROPOSAL".
- If a project proposal is pending review (PROJECT_REVIEW state) and user asks to modify the project name or description:
  Delegate to Repo_Setup_Agent with operation: "UPDATE_PROJECT_PROPOSAL".
- If a project proposal is pending review (PROJECT_REVIEW state) and the user explicitly approves/confirms creating the project (e.g. "looks good, create the project", "proceed with creation", "approved"):
  Delegate to Repo_Setup_Agent with operation: "CREATE_PROJECT".
- If the project was created (PROJECT_CREATED / REPOSITORY_DECISION state) and user asks/confirms to proceed with repository creation:
  Delegate to Repo_Setup_Agent with operation: "GENERATE_REPOSITORY_STRUCTURE".
- If repository structure is pending review (REPOSITORY_REVIEW state) and user requests changes to folders or files:
  Delegate to Repo_Setup_Agent with operation: "UPDATE_REPOSITORY_STRUCTURE".
- If repository structure is pending review (REPOSITORY_REVIEW state) and user explicitly approves creating the repository (e.g. "structure looks great, go ahead and create the repo", "approved"):
  Delegate to Repo_Setup_Agent with operation: "CREATE_REPOSITORY".
- If user explicitly decides to skip or cancel repository creation:
  Do not delegate; answer directly acknowledging the decision and concluding the workflow.

C. CONVERSATIONAL / INFORMATIONAL:
- If the user query is generic, conversational, informational, or does not require child agent functionality, answer directly and do NOT invoke any child agent.

When a user starts a new conversation:
- You MUST prefix your very first response with "TITLE: <a short 3-5 word title describing the user's project>\\n\\n". Do not include this prefix in subsequent messages.

DELEGATION INSTRUCTION:
- If Requirements_Agent is required, output:
  "DELEGATE_TO: Requirements_Agent | WORKFLOW: <PRD or TRD> | OPERATION: <GENERATE_PRD, UPDATE_PRD, GENERATE_TRD, or UPDATE_TRD>"
- If Repo_Setup_Agent is required, output:
  "DELEGATE_TO: Repo_Setup_Agent | OPERATION: <GENERATE_PROJECT_PROPOSAL, UPDATE_PROJECT_PROPOSAL, CREATE_PROJECT, GENERATE_REPOSITORY_STRUCTURE, UPDATE_REPOSITORY_STRUCTURE, or CREATE_REPOSITORY>"
- If no child agent is required, provide your direct, helpful response to the user.
"""

        orchestrator = Agent(
            name="Orchestrator",
            instructions=main_agent_prompt,
            client=client,
        )

        requirements_agent = get_requirements_agent(client)
        repo_setup_agent = get_repo_setup_agent(client)

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
        is_req_delegation = "Requirements_Agent" in response_text or "DELEGATE_TO: Requirements_Agent" in response_text
        is_repo_delegation = "Repo_Setup_Agent" in response_text or "DELEGATE_TO: Repo_Setup_Agent" in response_text

        if is_req_delegation:
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

        elif is_repo_delegation:
            logger.info(f"[Routing Decision] -> Delegating to Repo_Setup_Agent")

            # Extract operation from Orchestrator's decision
            operation = "GENERATE_PROJECT_PROPOSAL"
            if "OPERATION:" in response_text:
                operation = response_text.split("OPERATION:", 1)[1].strip()

            workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {operation}]"

            # Prepare message list for Repo_Setup_Agent with clear workflow context
            repo_messages = list(message_list[:-1])
            context_user_msg = Message(role="user", contents=[user_query + workflow_context_note])
            repo_messages.append(context_user_msg)

            # Invoke Repo Setup Child Agent
            logger.info(f"[Repo_Setup_Agent] Processing request with operation: {operation}...")
            child_start = time.time()
            child_response = await repo_setup_agent.run(messages=repo_messages)
            child_duration = round((time.time() - child_start) * 1000, 1)
            child_raw_json = child_response.messages[-1].text if child_response.messages else ""
            logger.info(f"[Repo_Setup_Agent] Completed in {child_duration}ms")

            # Orchestrator Synthesizer for Repo_Setup_Agent
            logger.info("[Orchestrator] Converting Repo_Setup_Agent output into user-facing natural language...")
            synthesis_prompt = f"""
You are the DevFoundry Orchestrator.
The Repo_Setup_Agent has produced the following structured output:
{child_raw_json}

Your task:
Convert this output into a friendly, structured, and professional response for the user:

CASE 1: If status is "ERROR":
- Clearly and politely explain what went wrong based on the error details (do NOT expose any secrets or tokens).
- Acknowledge that the project proposal or repository structure remains saved.
- Guide the user on what corrective steps are needed or offer to retry.

CASE 2: If status is "COMPLETE" and workflow_stage is "PROJECT_REVIEW" (or contains project_proposal):
- Present the proposed Azure DevOps project details using clean Markdown:
  * **Proposed Project Name**
  * **Project Description**
- Explain that this proposal is based on the project requirements and architecture.
- Clearly invite the user to review: they can request modifications to the name/description or explicitly approve it to proceed with creating the Azure DevOps project.

CASE 3: If status is "COMPLETE" and workflow_stage is "PROJECT_CREATED" (or contains project details with id/url):
- Announce that the Azure DevOps project has been created successfully!
- Show the project details clearly:
  * **Project Name**: [name]
  * **Project ID**: [id]
  * **Project URL**: [link](url)
- Naturally present the next decision to the user by concluding with:
  "**Would you like to proceed with repository creation?**"

CASE 4: If status is "COMPLETE" and workflow_stage is "REPOSITORY_REVIEW" (or contains repository_structure):
- Present the proposed repository directory structure in a clear Markdown code block.
- Briefly explain how the proposed structure accommodates the project's technology stack (e.g., frontend, backend, configuration, and docs).
- Clearly invite the user to review: they can request adjustments to folders or files, or explicitly approve it so we can create the repository.

CASE 5: If status is "COMPLETE" and workflow_stage is "REPOSITORY_CREATED" (or contains repository details with id/url):
- Announce that the Azure DevOps Git repository has been created successfully!
- Show the repository details clearly:
  * **Repository Name**: [name]
  * **Repository ID**: [id]
  * **Repository URL**: [link](url)
- Conclude by stating that repository setup is complete and the project is ready for development.

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

async def process_chat_request(conversation_id: Optional[str], user_query: str, file: Optional[UploadFile] = None) -> ChatResponse:
    overall_start = time.time()

    # If conversation_id is not provided, create a new conversation for this initial message
    if not conversation_id or str(conversation_id).strip().lower() in ("null", "none", ""):
        logger.info("[Conversation] No conversation_id provided; creating new conversation row for initial message.")
        new_conv = create_new_conversation(title="New Project")
        conversation_id = new_conv.conversation_id
        conv = {"conv_id": conversation_id, "title": "New Project", "content": []}
    else:
        conv = get_conversation_by_id(conversation_id)
        if not conv:
            logger.warning(f"[Supabase] Conversation '{conversation_id}' not found in DB.")
            raise ValueError(f"Conversation '{conversation_id}' not found.")

    logger.info(f"=== Starting chat processing pipeline for conversation '{conversation_id}' ===")
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


