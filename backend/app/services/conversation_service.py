import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional
from fastapi import UploadFile
from dotenv import load_dotenv

from agent_framework import Agent, Message
from agent_framework.openai import OpenAIChatCompletionClient
from supabase import create_client, Client

from app.schemas.conversation import ChatResponse, ConversationItem
from app.services.agents.requirements_agent import process_uploaded_document, get_requirements_agent
from app.services.agents.repo_setup_agent import get_repo_setup_agent
from app.services import project_store as store
from app.services.repo_structure import render_structure_tree
from app.services.workflow_routing import (RoutingDecision, blocked_reply, effective_stage, next_steps,
                                           parse_routing_decision, routing_context, validate_decision,
                                           apply_intent_rules)
from app.core.logger import logger

from app.services.llm_client import create_fallback_client

# Load variables from .env file (checks repo root, backend folder, and default)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../../.env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env"))
load_dotenv() # also try default location

_warned_key_role = False

def supabase_key_role(key: str) -> str:
    """'service_role', 'anon' or 'unknown' (supports legacy JWT keys and the newer sb_secret_/sb_publishable_ keys)."""
    if key.startswith("sb_secret_"):
        return "service_role"
    if key.startswith("sb_publishable_"):
        return "anon"
    try:
        import base64
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role") or "unknown"
    except Exception:
        return "unknown"

def get_supabase_client() -> Client:
    """Backend-only Supabase client. Use the service-role key: the tables are closed to the anon key (migration 003)."""
    global _warned_key_role
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        logger.error("[Supabase] Credentials missing in environment (.env).")
        raise ValueError("Supabase credentials not found in environment.")
    if not _warned_key_role and supabase_key_role(key) != "service_role":
        _warned_key_role = True
        logger.warning("[Supabase] The configured key is not a service-role key. After migration 003 the tables are "
                       "closed to the anon key; set SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) to the service-role key.")
    return create_client(url, key)

def _resolve_project_stage(project_row: Optional[Dict[str, Any]]) -> str:
    """Derives a normalized stage key from Project table columns."""
    if not project_row:
        return "requirements"

    stage = project_row.get("workflow_stage") or ""
    repo_url = project_row.get("repo_url")
    ado_project_id = project_row.get("ado_project_id")
    has_trd = bool(project_row.get("trd"))
    has_prd = bool(project_row.get("prd"))

    if stage == "COMPLETE":
        return "completed"
    if repo_url or stage in ["STRUCTURE_REVIEW", "REPOSITORY_REVIEW"]:
        return "repo_created"
    if ado_project_id or stage == "PROJECT_REVIEW":
        return "project_created"
    if has_trd or stage == "TRD_REVIEW":
        return "trd_created"
    if has_prd or stage == "PRD_REVIEW":
        return "prd_created"
    return "requirements"

_in_memory_conversations: Dict[str, Dict[str, Any]] = {}

def get_conversations(user_id: str) -> List[ConversationItem]:
    """Conversations owned by the signed-in user, newest first."""
    supabase = get_supabase_client()
    try:
        response = (supabase.table("Conversation").select("*").eq("user_id", user_id)
                    .order("created_at", desc=True).execute())
        conv_rows = response.data or []

        # Batch lookup stage information from Project table
        projects_by_conv: Dict[str, dict] = {}
        conv_ids = [r["conv_id"] for r in conv_rows if r.get("conv_id")]
        if conv_ids:
            try:
                proj_res = (
                    supabase.table("Project")
                    .select("conv_id, workflow_stage, prd, trd, ado_project_id, repo_url")
                    .in_("conv_id", conv_ids)
                    .execute()
                )
                for p_row in (proj_res.data or []):
                    c_id = p_row.get("conv_id")
                    if c_id:
                        projects_by_conv[c_id] = p_row
            except Exception as pe:
                logger.warning(f"[Supabase] Could not batch-fetch project stages: {pe}")

        items = []
        for row in conv_rows:
            conv_id = row.get("conv_id")
            title = row.get("title", "New Project")
            if conv_id:
                stage = _resolve_project_stage(projects_by_conv.get(conv_id))
                items.append(ConversationItem(conversation_id=conv_id, title=title, stage=stage))
        logger.info(f"[Supabase] Retrieved {len(items)} conversations for user '{user_id}' from DB.")
        return items
    except Exception as e:
        logger.warning(f"[Supabase] Error fetching conversations: {str(e)}; falling back to local memory store.")
        items = []
        for c_id, row in _in_memory_conversations.items():
            if row.get("user_id") == user_id:
                items.append(ConversationItem(
                    conversation_id=c_id,
                    title=row.get("title", "New Project"),
                    stage="requirements",
                ))
        return items

def create_new_conversation(user_id: str, title: str = "New Project") -> ConversationItem:
    supabase = get_supabase_client()
    new_id = str(uuid.uuid4())
    data = {
        "conv_id": new_id,
        "user_id": user_id,
        "content": [],
        "title": title
    }
    try:
        supabase.table("Conversation").insert(data).execute()
        logger.info(f"[Supabase] Created new conversation '{new_id}' (Title: '{title}')")
        _in_memory_conversations[new_id] = data
        return ConversationItem(conversation_id=new_id, title=title, stage="requirements")
    except Exception as e:
        logger.error(f"[Supabase] Failed to create new conversation: {str(e)}")
        raise

def delete_conversation(conversation_id: str, user_id: str) -> bool:
    """Deletes the conversation if the user owns it (its Project/Repository rows cascade). False if not found/owned."""
    if not get_conversation_by_id(conversation_id, user_id):
        return False

    _in_memory_conversations.pop(conversation_id, None)
    supabase = get_supabase_client()
    try:
        supabase.table("Conversation").delete().eq("conv_id", conversation_id).eq("user_id", user_id).execute()
        logger.info(f"[Supabase] Deleted conversation '{conversation_id}'")
        return True
    except Exception as e:
        logger.error(f"[Supabase] Failed to delete conversation '{conversation_id}': {str(e)}")
        raise


def get_conversation_by_id(conversation_id: str, user_id: str) -> Optional[Dict]:
    """The conversation if it exists AND belongs to the user; None otherwise (callers answer 404 either way)."""
    try:
        uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return None

    supabase = get_supabase_client()
    try:
        response = (supabase.table("Conversation").select("*").eq("conv_id", conversation_id)
                    .eq("user_id", user_id).execute())
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        logger.error(f"[Supabase] Error retrieving conversation '{conversation_id}': {str(e)}")
        raise



# Agent Framework Client configuration (Tiered Fallback: Azure OpenAI -> Groq -> MAQ AI SLM)

def get_agent_framework_client() -> OpenAIChatCompletionClient:
    return create_fallback_client()

# Workflow state persistence (Project / Repository tables)

STRUCTURE_PLACEHOLDER = "[[REPOSITORY_STRUCTURE]]"
STRUCTURE_STAGES = ("REPOSITORY_CREATED", "STRUCTURE_REVIEW", "STRUCTURE_PUSHED")

def parse_agent_json(text: str) -> Optional[Dict]:
    """Extracts the JSON object a child agent returned (tolerates ```json fences)."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None

def missing_readiness_items(raw_output: str) -> List[str]:
    """R1-R5 items the Requirements_Agent itself marked "missing" in a PRD it nevertheless returned as COMPLETE."""
    data = parse_agent_json(raw_output) or {}
    readiness = data.get("readiness")
    if data.get("status") != "COMPLETE" or not isinstance(readiness, dict):
        return []
    return sorted(key for key, value in readiness.items() if str(value).strip().lower() == "missing")

def persist_requirements_output(conversation_id: Optional[str], raw_output: str) -> None:
    """Stores a completed PRD/TRD in the Project row of the conversation."""
    data = parse_agent_json(raw_output)
    if not conversation_id or not data or data.get("status") != "COMPLETE":
        return
    prd = data.get("structured_project_response")
    trd = data.get("technical_requirements_document")
    if not prd and not trd:
        return
    try:
        project = store.ensure_project(conversation_id)
        stage = project.get("workflow_stage") or store.STAGE_REQUIREMENTS
        versioned = "prd_version" in project  # migration 002 applied
        fields: Dict = {}
        prd_version = project.get("prd_version") or 0
        if prd:
            fields["prd"] = prd
            if versioned:
                prd_version += 1
                fields["prd_version"] = prd_version
            # Documents can change at any stage; only the requirements stages move forward here.
            if stage in (store.STAGE_REQUIREMENTS, store.STAGE_PRD_REVIEW):
                fields["workflow_stage"] = store.STAGE_PRD_REVIEW
        if trd:
            fields["trd"] = trd
            if versioned:
                fields["trd_version"] = (project.get("trd_version") or 0) + 1
                fields["trd_prd_version"] = prd_version
            if stage in (store.STAGE_REQUIREMENTS, store.STAGE_PRD_REVIEW, store.STAGE_TRD_REVIEW):
                fields["workflow_stage"] = store.STAGE_TRD_REVIEW
        store.update_project(project["project_id"], **fields)
        logger.info(f"[ProjectStore] Saved {', '.join(fields).upper()} for conversation '{conversation_id}'")
    except store.StoreError as e:
        logger.error(f"[ProjectStore] Could not save requirements: {e}")

def persist_repo_setup_output(conversation_id: Optional[str], raw_output: str) -> None:
    """Stores project/repository proposals from the Repo_Setup_Agent. Created resources are stored by its tools."""
    data = parse_agent_json(raw_output)
    if not conversation_id or not data or data.get("status") != "COMPLETE":
        return
    stage = data.get("workflow_stage")
    try:
        proposal = data.get("project_proposal") or {}
        if stage == "PROJECT_REVIEW" and proposal.get("project_name"):
            project = store.ensure_project(conversation_id)
            if not project.get("ado_project_id"):
                versions = {"proposal_trd_version": project.get("trd_version") or 0} if "trd_version" in project else {}
                store.update_project(project["project_id"], project_name=proposal["project_name"].strip(),
                                     project_description=(proposal.get("project_description") or "").strip(),
                                     workflow_stage=store.STAGE_PROJECT_REVIEW, **versions)
        repo_proposal = data.get("repository_proposal") or {}
        if stage in ("PROJECT_CREATED", "REPOSITORY_REVIEW") and (repo_proposal.get("repo_name") or "").strip():
            project = store.get_project(conversation_id)
            if project and project.get("ado_project_id"):
                store.propose_repository_name(project["project_id"], repo_proposal["repo_name"].strip())
                store.update_project(project["project_id"], workflow_stage=store.STAGE_REPOSITORY_REVIEW)
    except store.StoreError as e:
        logger.error(f"[ProjectStore] Could not save repository setup proposal: {e}")

def follow_up_note(conversation_id: Optional[str]) -> str:
    """Outdated artifacts after this turn, for the synthesizer to mention (empty when nothing is outdated)."""
    if not conversation_id:
        return ""
    state = store.get_workflow_state(conversation_id)
    items = store.outdated_items(state) if not state.get("error") else []
    if not items:
        return ""
    lines = "\n".join(f"- {store.OUTDATED_DESCRIPTIONS[item]}" for item in items)
    return ("\n\nFOLLOW-UPS: Because documents changed, these items are now outdated:\n" + lines +
            "\nEnd your answer with a short '**Suggested next steps**' list offering to update them (in that order), "
            "instead of moving the workflow forward.")

def render_structure_for_display(state: Dict) -> Optional[str]:
    """Renders the stored structure (the exact files that are / will be pushed) as Markdown."""
    repository = (state or {}).get("repository") or {}
    files = repository.get("structure") or []
    if not files:
        return None
    pushed = repository.get("status") == store.REPO_PUSHED
    title = "Pushed repository structure" if pushed else "Proposed repository structure"
    header = f"**{title}** (version {repository.get('structure_version')}, {len(files)} files)"
    return header + "\n\n" + render_structure_tree(files, root_name=repository.get("repo_name"))

def insert_structure_tree(text: str, tree_markdown: Optional[str]) -> str:
    """Replaces the synthesizer's placeholder with the code-rendered tree (appends it if the placeholder is missing)."""
    if not tree_markdown:
        return text.replace(STRUCTURE_PLACEHOLDER, "")
    if STRUCTURE_PLACEHOLDER in text:
        return text.replace(STRUCTURE_PLACEHOLDER, tree_markdown, 1).replace(STRUCTURE_PLACEHOLDER, "")
    return text.rstrip() + "\n\n" + tree_markdown

async def generate_chat_response(user_query: str, history: List[Dict], conversation_id: Optional[str] = None) -> str:
    """Uses Microsoft Agent Framework to generate a response with conversation history."""
    try:
        client = get_agent_framework_client()
        deployment = getattr(client, "current_model", None) or getattr(client, "model", None) or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
        provider_name = getattr(client, "current_provider_name", "Primary")

        main_agent_prompt = """
You are DevFoundry, an AI-powered software development orchestration assistant.

For every user message you decide ONE thing: answer the user directly, or delegate exactly ONE operation to ONE child agent.
You return a structured decision (reasoning, action, agent, operation, reply, title). You never write PRDs, TRDs or proposals yourself.

AVAILABLE CHILD AGENTS AND OPERATIONS:
1. Requirements_Agent
   - GENERATE_PRD: create the Product Requirements Document from a project description, an uploaded document, or answers to clarifying questions.
   - UPDATE_PRD: change functional requirements (features, users, roles, flows, goals) of an existing PRD.
   - GENERATE_TRD: create the Technical Requirements Document from the PRD.
   - UPDATE_TRD: change, add, remove or confirm technologies in an existing TRD.
2. Repo_Setup_Agent
   - GENERATE_PROJECT_PROPOSAL / UPDATE_PROJECT_PROPOSAL: propose / change the Azure DevOps project name and description (before it exists).
   - CREATE_PROJECT: create the Azure DevOps project (explicit approval only); it also suggests a repository name.
   - UPDATE_ADO_PROJECT: rename / change the description of the already created Azure DevOps project.
   - GENERATE_REPOSITORY_PROPOSAL / UPDATE_REPOSITORY_PROPOSAL: propose / rename the repository.
   - CREATE_REPOSITORY: create the empty repository (explicit approval only); it also proposes the folder structure.
   - GENERATE_REPOSITORY_STRUCTURE / UPDATE_REPOSITORY_STRUCTURE: propose a new structure / change the shown structure.
   - PUSH_REPOSITORY_STRUCTURE: push the approved structure to the main branch (explicit approval only); if it was pushed
     before, only the changes are pushed as a follow-up commit.

WORKFLOW STAGES (in order):
REQUIREMENTS -> PRD_REVIEW -> TRD_REVIEW -> PROJECT_REVIEW -> REPOSITORY_REVIEW -> STRUCTURE_REVIEW -> COMPLETE
The system appends a "[Current Workflow State (database)]" block to the user's latest message with current_stage and
allowed_operations. It is authoritative. You may ONLY delegate an operation listed in allowed_operations.

ROUTING RULES:
1. Never skip a step. Moving to the next step requires the user to EXPLICITLY ask for that next step.
   - PRD -> TRD: delegate GENERATE_TRD only if the user explicitly asks for the TRD (e.g. "create the TRD",
     "go ahead with the technical requirements", or "yes" as a direct answer to the question "Would you like me to create a TRD?").
     Approving or praising the PRD ("the PRD looks good", "PRD approved") is NOT a request for the TRD:
     respond, confirming the PRD is approved, and ask whether you should create the TRD now.
   - TRD -> repository setup: delegate GENERATE_PROJECT_PROPOSAL only if the user explicitly asks to proceed with repository /
     Azure DevOps project setup (e.g. "let's proceed with repository setup", "set up the ADO project").
     Approving the TRD alone ("the TRD looks good") is NOT that request: respond, confirming the TRD is final, and ask
     whether to proceed with repository setup.
2. The PRD and TRD can be updated at ANY stage, even after the Azure DevOps project, repository or push exist.
   - Any message about technologies (use, add, replace, remove or keep a language, framework, database, library or cloud
     service) is UPDATE_TRD, EVEN IF the TRD already contains that technology. Never treat it as approval to move on.
   - Changes to features, users, roles or flows -> UPDATE_PRD.
   Updating one document never updates the next one automatically: the state block then lists "outdated" items and the
   user is offered to update them.
3. Outdated items (see "outdated" in the state block) are updated only when the user asks for it:
   - "update the TRD accordingly" (TRD_OUTDATED) -> UPDATE_TRD.
   - project name/description (PROJECT_PROPOSAL_OUTDATED, before creation) -> UPDATE_PROJECT_PROPOSAL;
     (ADO_PROJECT_OUTDATED, after creation, or any rename request) -> UPDATE_ADO_PROJECT.
   - repository structure (STRUCTURE_OUTDATED) -> UPDATE_REPOSITORY_STRUCTURE; pushing the updated structure again -> PUSH_REPOSITORY_STRUCTURE.
4. A project description, an uploaded document, answers to clarifying questions, or a request to proceed with assumptions
   ("make reasonable assumptions", "just generate it") -> GENERATE_PRD (no PRD yet) or UPDATE_PRD. Even a vague one-line
   description goes to GENERATE_PRD: the Requirements_Agent decides whether it needs to ask questions first.
5. Project / repository / structure steps:
   - PROJECT_REVIEW: name/description changes -> UPDATE_PROJECT_PROPOSAL; explicit approval to create -> CREATE_PROJECT.
   - REPOSITORY_REVIEW: different repository name -> UPDATE_REPOSITORY_PROPOSAL; explicit approval to create -> CREATE_REPOSITORY;
     no repository name proposed yet -> GENERATE_REPOSITORY_PROPOSAL.
   - STRUCTURE_REVIEW: changes to folders/files -> UPDATE_REPOSITORY_STRUCTURE; a completely new structure -> GENERATE_REPOSITORY_STRUCTURE;
     explicit approval to push (optionally with a commit message) -> PUSH_REPOSITORY_STRUCTURE.
6. COMPLETE: the structure was pushed. Further structure changes -> UPDATE_REPOSITORY_STRUCTURE (pushed later as a follow-up
   commit after explicit approval); project rename/description -> UPDATE_ADO_PROJECT; otherwise respond directly.
7. Never "re-push" when nothing changed: if the user asks to push again and the state shows no unpushed changes, respond.
8. Greetings, general questions, status questions, or a request to skip/cancel a step -> respond directly.
9. If you cannot tell whether the user is approving, asking for a change, or asking for the next step, respond with ONE short
   clarifying question instead of delegating.

DECISION FIELDS:
- reasoning: one short sentence (for logs).
- user_intent: classify the latest user message on its own (approve_only, explicit_next_step, change_request, question, other).
  "The PRD/TRD looks good" or "approved" is approve_only. "yes" in direct answer to an offer of the next step is explicit_next_step.
  An instruction such as "Use PostgreSQL as the database" is change_request even if the TRD already uses it.
- mentions_technology: true if the message names a language, framework, database, library or cloud/infrastructure service.
- action: "delegate" or "respond".
- agent + operation: required when delegating (operation must be in allowed_operations).
- reply: required when responding. Friendly, concise Markdown that ends with the clear next step the user can take.
- title: only for the very first user message of a new conversation (there are no previous messages): a 3-5 word project title. Otherwise null.
"""

        orchestrator = Agent(
            name="Orchestrator",
            instructions=main_agent_prompt,
            client=client,
        )

        requirements_agent = get_requirements_agent(client)
        repo_setup_agent = get_repo_setup_agent(client, conversation_id)

        # Stored workflow state (Project / Repository tables) for routing and child agents. Never persisted in chat history.
        workflow_state = store.get_workflow_state(conversation_id) if conversation_id else None
        stage = effective_stage(workflow_state)
        state_note = "\n\n" + routing_context(workflow_state)

        # Convert history into Agent Framework Messages
        message_list = []
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            message_list.append(Message(role=role, contents=[text]))

        new_msg = Message(role="user", contents=[user_query])
        message_list.append(new_msg)

        # 1. Execute the Orchestrator: a structured routing decision (Agent Framework response_format)
        logger.info(f"[Orchestrator] Running Orchestrator Agent (Provider: {provider_name}, Model: {deployment}, History: {len(history)} messages, Stage: {stage})...")
        orch_start = time.time()
        orchestrator_messages = list(message_list[:-1]) + [Message(role="user", contents=[user_query + state_note])]
        orchestrator_response = await orchestrator.run(messages=orchestrator_messages,
                                                       options={"response_format": RoutingDecision})
        orch_duration = round((time.time() - orch_start) * 1000, 1)
        decision = parse_routing_decision(orchestrator_response)
        logger.info(f"[Orchestrator] Completed in {orch_duration}ms -> {decision.action} "
                    f"{decision.agent or ''} {decision.operation or ''} | intent={decision.user_intent}, "
                    f"tech={decision.mentions_technology} | {decision.reasoning}")

        title_prefix = ""
        if decision.title and decision.title.strip() and not history:
            title_prefix = f"TITLE: {decision.title.strip()}\n\n"

        # 2. Workflow guard: the operation must be allowed in the current stage
        corrected = apply_intent_rules(decision, stage, workflow_state)
        if corrected is not decision:
            logger.warning(f"[Routing Guard] {decision.action} {decision.operation or ''} -> {corrected.action} "
                           f"{corrected.operation or ''} ({corrected.reasoning})")
            decision = corrected
        blocked = validate_decision(decision, stage, workflow_state)
        if blocked:
            logger.warning(f"[Routing Guard] {decision.operation} is not allowed in stage {stage}; answering directly")
            decision = RoutingDecision(reasoning="blocked by workflow guard", action="respond",
                                       reply=blocked_reply(stage, blocked))
        route_agent = decision.agent if decision.action == "delegate" else None
        operation = decision.operation
        response_text = decision.reply or blocked_reply(stage, "")

        if route_agent == "Requirements_Agent":
            logger.info(f"[Routing Decision] -> Delegating to Requirements_Agent ({operation})")

            workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {operation}]"
            if workflow_state is not None and workflow_state.get("project"):
                workflow_context_note += "\n\n" + store.describe_workflow_state(workflow_state, include_requirements=True)

            # Prepare message list for Requirements_Agent with clear workflow context
            req_messages = list(message_list[:-1])
            context_user_msg = Message(role="user", contents=[user_query + workflow_context_note])
            req_messages.append(context_user_msg)

            # Invoke Requirements Child Agent
            logger.info(f"[Requirements_Agent] Processing request with operation: {operation}...")
            child_start = time.time()
            child_response = await requirements_agent.run(messages=req_messages)
            child_duration = round((time.time() - child_start) * 1000, 1)
            child_raw_json = child_response.messages[-1].text if child_response.messages else ""
            logger.info(f"[Requirements_Agent] Completed in {child_duration}ms")

            # Readiness guard: a new PRD is only accepted when the agent's own readiness check has no missing item
            missing = missing_readiness_items(child_raw_json) if operation == "GENERATE_PRD" else []
            if missing:
                logger.warning(f"[Readiness Guard] PRD written although {', '.join(missing)} missing; asking again")
                retry_messages = req_messages + [
                    Message(role="assistant", contents=[child_raw_json]),
                    Message(role="user", contents=[
                        f"[Readiness guard] Your readiness check marks {', '.join(missing)} as missing, so the PRD must not be "
                        "written yet. Return CLARIFICATION_REQUIRED asking only for the missing REQUIRED items."]),
                ]
                child_response = await requirements_agent.run(messages=retry_messages)
                child_raw_json = child_response.messages[-1].text if child_response.messages else child_raw_json

            persist_requirements_output(conversation_id, child_raw_json)
            follow_ups = follow_up_note(conversation_id)

            # 3. Follow-Up Flow: Child Agent -> Orchestrator Synthesizer -> User
            logger.info("[Orchestrator] Converting Requirements_Agent output into user-facing natural language...")
            synthesis_prompt = f"""
You are the DevFoundry Orchestrator. 
The Requirements_Agent has produced the following structured output:
{child_raw_json}

Your task:
Convert this output into a friendly, structured, and professional response for the user:

CASE 1: If status is "CLARIFICATION_REQUIRED":
- Start with one sentence explaining that a few details are needed before the document can be written, then summarize
  "known_so_far" in one short sentence.
- Present the questions as a numbered list in the given order: the question in **bold**; when it has "choices", list them as
  bullets indented under the question. Do not show the "reason" field verbatim; you may use it to phrase the question clearly.
- End with: "You can answer in one message (for example "1: ..., 2: ..."), or say **"make reasonable assumptions"** and I'll
  draft the document with the assumptions clearly listed."

CASE 2: If status is "COMPLETE" and workflow_stage is "PRD" (or contains structured_project_response):
- Start with ONE introductory sentence before the first heading, e.g. "Here is the **Product Requirements Document (PRD)**
  based on your project description:" (say "based on the uploaded document" if the requirements came from an attached file).
  For an updated PRD (change_notes present) say "Here is the **updated Product Requirements Document (PRD)**:" and then
  summarize what changed in one short "**What changed:**" paragraph based on change_notes.
- Present the complete PRD with a Markdown heading per section, in this order (skip a section only if it is missing):
  * **Objective**, **Problem Statement** (paragraphs)
  * **Target Users**, **Goals** (bulleted lists)
  * **Functional Requirements** (bulleted list, "**FR-001:** The system shall ...")
  * **User Experience** (paragraph), **User Flow** (numbered list)
  * **Non-Goals**, **Constraints** (bulleted lists; write "None stated" when empty)
  * **Assumptions** (bulleted list; only when not empty, introduced with "These were assumed - please confirm or correct them:")
  * **Open Questions** (bulleted list; only when not empty, introduced with "Still open - you can answer these anytime:")
- Naturally offer the next logical workflow step by concluding with:
  "**Would you like me to create a Technical Requirements Document (TRD) as well?**"

CASE 3: If status is "COMPLETE" and workflow_stage is "TRD" (or contains technical_requirements_document):
- Start with ONE introductory sentence before the first heading, e.g. "Here is the **Technical Requirements Document (TRD)**
  based on the approved PRD:" (or "Here is the **updated Technical Requirements Document (TRD)**:" when change_notes is present).
- Present the comprehensive technical stack recommendations using clean Markdown headings:
  * **Frontend Technology Stack** (Framework, Supporting Technologies, Tools & Libraries, Rationale)
  * **Backend Technology Stack** (Language, Framework, API Technologies, Supporting Technologies, Rationale)
  * **Database Technology Stack** (Database, Database Type, Supporting Technologies, Rationale)
  * **Integration & Infrastructure** (External Integrations, File Storage, Messaging, Deployment, CI/CD & Observability, Rationale)
  * **Security** (Authentication & Authorization, Data Protection, Application Security, Compliance, Rationale)
- Under each heading use ONE flat bullet per field, e.g. "- **Framework:** React" and "- **Supporting Technologies:** Redux Toolkit, React Router".
  Do not nest lists. Put the rationale in its own short paragraph starting with "**Rationale:**".
- If change_notes is present, start with a short "**What changed:**" paragraph based on change_notes (including when nothing needed to change).
- Conclude by asking if the user would like to make any modifications to these technology choices, or if they are ready to proceed with repository setup.

Do NOT output raw JSON. Output only clean, beautifully formatted Markdown text.
Put a blank line before and after every heading, divider (---) and list; never put "---" at the end of a text line.
{follow_ups}
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

        elif route_agent == "Repo_Setup_Agent":
            logger.info(f"[Routing Decision] -> Delegating to Repo_Setup_Agent ({operation})")

            workflow_context_note = f"\n[Workflow Context from Orchestrator: Operation = {operation}]"
            if workflow_state is not None:
                workflow_context_note += "\n\n" + store.describe_workflow_state(
                    workflow_state, include_requirements=True, include_structure=True)

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
            persist_repo_setup_output(conversation_id, child_raw_json)
            follow_ups = follow_up_note(conversation_id)

            # The structure shown to the user is rendered from the stored files (the same files that get pushed)
            child_data = parse_agent_json(child_raw_json) or {}
            structure_tree = None
            if conversation_id and child_data.get("workflow_stage") in STRUCTURE_STAGES:
                structure_tree = render_structure_for_display(store.get_workflow_state(conversation_id))

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
- Acknowledge that the saved proposal or repository structure remains saved.
- Guide the user on what corrective steps are needed or offer to retry.

CASE 2: If status is "COMPLETE" and workflow_stage is "PROJECT_REVIEW" (or contains project_proposal):
- Present the proposed Azure DevOps project details using clean Markdown:
  * **Proposed Project Name**
  * **Project Description**
- Explain that this proposal is based on the project requirements and architecture.
- Clearly invite the user to review: they can request modifications to the name/description or explicitly approve it to proceed with creating the Azure DevOps project.

CASE 3: If status is "COMPLETE" and workflow_stage is "PROJECT_CREATED":
- Announce that the Azure DevOps project has been created successfully!
- Show the project details clearly:
  * **Project Name**: [name]
  * **Project ID**: [id]
  * **Project URL**: [link](url)
- Present the next step: the suggested repository name (**Suggested Repository Name**: repository_proposal.repo_name).
- Conclude by asking: "**Shall I create the repository with this name, or would you prefer a different name?**"

CASE 4: If status is "COMPLETE" and workflow_stage is "REPOSITORY_REVIEW":
- Present the **Proposed Repository Name** (repository_proposal.repo_name).
- Conclude by asking whether to create the repository with this name or use a different name.

CASE 5: If status is "COMPLETE" and workflow_stage is "REPOSITORY_CREATED":
- Announce that the (empty) Azure DevOps Git repository has been created successfully, with **Repository Name**, **Repository ID** and **Repository URL** ([link](url)).
- Briefly explain the proposed folder structure (structure.summary), then output the placeholder line (see STRUCTURE RULE).
- Invite the user to review it: they can request changes to folders/files, or approve it to push it to the main branch as a single initial commit.

CASE 6: If status is "COMPLETE" and workflow_stage is "STRUCTURE_REVIEW":
- Briefly state what changed (structure.summary), then output the placeholder line (see STRUCTURE RULE).
- Invite the user to request further changes or approve pushing it to the main branch as a single initial commit.

CASE 7: If status is "COMPLETE" and workflow_stage is "STRUCTURE_PUSHED":
- Announce that the approved structure was pushed as a single commit (say whether it is the initial commit or a follow-up
  commit; for a follow-up commit list the added, edited and deleted files from push.changes).
- Show **Repository**, **Commit ID**, **Commit Message**, **Branch**, **Files** (files_count) and a link to the commit (commit_url).
- Output the placeholder line (see STRUCTURE RULE), then state that repository setup is complete and the project is ready for development.

CASE 8: If status is "COMPLETE" and workflow_stage is "ADO_PROJECT_UPDATED":
- Announce that the Azure DevOps project was updated, with **Project Name**, **Project Description** and **Project URL** ([link](url)).

STRUCTURE RULE (CASES 5, 6, 7):
- Output the exact line {STRUCTURE_PLACEHOLDER} on its own line where the folder structure belongs. The system replaces it with the exact stored structure.
- NEVER draw the folder tree or list the files yourself.

Do NOT output raw JSON. Output only clean, beautifully formatted Markdown text.
Put a blank line before and after every heading, divider (---) and list; never put "---" at the end of a text line.
{follow_ups}
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
            response_text = insert_structure_tree(response_text, structure_tree)
            logger.info(f"[Orchestrator] Response synthesized ({synth_duration}ms)")

        else:
            logger.info("[Routing Decision] -> Handled directly by Orchestrator (Path A Generic/Conversational)")

        return title_prefix + response_text

    except Exception as e:
        if "content_filter" in str(e) or type(e).__name__ == "OpenAIContentFilterException":
            # Azure OpenAI content safety (incl. prompt-injection / jailbreak detection) rejected the input
            logger.warning(f"[Agent Pipeline] Blocked by the Azure OpenAI content filter: {type(e).__name__}")
            return ("⚠️ This message (or an attached document) was blocked by the AI content-safety filter, usually because it "
                    "contains text that looks like instructions to the AI (a possible prompt injection). Please remove that text "
                    "and try again. Nothing was changed.")
        logger.error(f"[Agent Pipeline] Error: {str(e)}")
        return f"Error communicating with Agent Framework: {str(e)}"

def get_suggestions(conversation_id: Optional[str]) -> List[Dict[str, str]]:
    """Next-step suggestions (label + prompt) for the chips above the chat input."""
    if not conversation_id:
        return []
    try:
        return next_steps(store.get_workflow_state(conversation_id))
    except Exception as e:
        logger.error(f"[Suggestions] Could not compute suggestions: {e}")
        return []

async def process_chat_request(conversation_id: Optional[str], user_query: str, file: Optional[UploadFile] = None,
                               *, user_id: str) -> ChatResponse:
    overall_start = time.time()

    # If conversation_id is not provided, create a new conversation for this initial message
    if not conversation_id or str(conversation_id).strip().lower() in ("null", "none", ""):
        logger.info("[Conversation] No conversation_id provided; creating new conversation row for initial message.")
        new_conv = create_new_conversation(user_id, title="New Project")
        conversation_id = new_conv.conversation_id
        conv = {"conv_id": conversation_id, "title": "New Project", "content": []}
    else:
        # Only the owner can continue a conversation; anyone else gets "not found"
        conv = get_conversation_by_id(conversation_id, user_id)
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
    response_text = await generate_chat_response(user_query, content_array, conversation_id)
    
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

    return ChatResponse(conversation_id=conversation_id, response=response_text,
                        suggestions=get_suggestions(conversation_id))


