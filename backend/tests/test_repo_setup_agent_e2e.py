"""
Opt-in end-to-end test of the complete repository setup flow through the real Orchestrator
(Azure OpenAI) + Supabase (Project/Repository tables) + Azure DevOps.

Run (from backend/, venv active), after running db/migrations/001_create_project_and_repository.sql:
    $env:ADO_E2E_TESTS="1"
    pytest -m e2e -s

It creates a "DevFoundry-E2E-<id>" ADO project and deletes it at the end (ADO_E2E_KEEP=1 keeps it),
and deletes its Project/Repository rows. No "Conversation" row is needed (generate_chat_response is called directly).
"""
import asyncio
import os
import sys

import pytest

from tests import ado_live as live

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("ADO_E2E_TESTS") != "1", reason="Set ADO_E2E_TESTS=1 to run"),
]

from app.services import conversation_service as cs  # noqa: E402
from app.services import project_store as store  # noqa: E402

PRD = {
    "Objective": "A REST API that lets small teams track inventory across warehouses.",
    "Problem Statement": "Inventory is tracked in spreadsheets, causing stock errors.",
    "Goals": ["Real-time stock levels", "Audit trail of stock movements"],
    "User Experience": "API-first; consumed by an existing internal admin UI.",
    "User Flow": ["Create warehouse", "Add items", "Record stock movements", "Query stock levels"],
}
TRD = {
    "Backend Technology Stack": {"language": "Go", "framework": "Gin", "api_technologies": ["REST"],
                                 "supporting_technologies": ["sqlc", "golang-migrate", "zap"],
                                 "rationale": "Fast, simple, statically compiled service."},
    "Database Technology Stack": {"database": "PostgreSQL", "database_type": "Relational",
                                  "supporting_technologies": ["pgx"], "rationale": "Relational stock data."},
}


def show(*parts) -> None:
    """Prints evidence without failing on characters the console encoding can't represent (e.g. emoji on cp1252)."""
    text = " ".join(str(p) for p in parts)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding))


async def turn(conv_id: str, history: list, user_text: str) -> str:
    response = await cs.generate_chat_response(user_text, history, conv_id)
    if response.startswith("TITLE:"):
        response = response.split("\n\n", 1)[-1]
    assert not response.startswith("Error communicating"), response
    history.append({"role": "user", "text": user_text})
    history.append({"role": "assistant", "text": response})
    show(f"\n===== USER: {user_text}\n----- ASSISTANT:\n{response}")
    return response


def state(conv_id: str) -> dict:
    s = store.get_workflow_state(conv_id)
    assert not s.get("error"), s
    return s


def test_full_repository_setup_flow():
    asyncio.run(_scenario())


async def _scenario():
    conv_id = live.unique("e2e-conv")
    project_name = live.unique("DevFoundry-E2E")
    repo_name = "e2e-inventory-api"
    project = store.ensure_project(conv_id)
    store.update_project(project["project_id"], prd=PRD, trd=TRD)
    history = [
        {"role": "user", "text": "I want an inventory tracking REST API for warehouses."},
        {"role": "assistant", "text": "The PRD is complete. **Would you like me to create a Technical Requirements Document (TRD) as well?**"},
        {"role": "user", "text": "yes"},
        {"role": "assistant", "text": "Here is the TRD: Go + Gin REST API, PostgreSQL with pgx, sqlc, golang-migrate, zap. "
                                      "Would you like to modify these technology choices, or are you ready to proceed with repository setup?"},
    ]
    ado_project_id = None
    try:
        # 1. Project proposal
        await turn(conv_id, history, "I'm happy with the TRD. Let's proceed with repository setup.")
        s = state(conv_id)
        assert s["project"]["workflow_stage"] == store.STAGE_PROJECT_REVIEW and s["project"]["project_name"]

        # 2. Update the proposal (name only)
        await turn(conv_id, history, f"Please change the project name to '{project_name}' and keep the description.")
        s = state(conv_id)
        assert s["project"]["project_name"] == project_name and not s["project"]["ado_project_id"]

        # 3. Create the project -> repository name suggested
        await turn(conv_id, history, "Looks good, create the project.")
        s = state(conv_id)
        ado_project_id = s["project"]["ado_project_id"]
        assert ado_project_id == live.wait_for_project(project_name, timeout=10)    # real project id stored
        assert s["project"]["workflow_stage"] == store.STAGE_REPOSITORY_REVIEW
        assert s["repository"] and s["repository"]["status"] == store.REPO_PROPOSED

        # 4. Rename the repository
        await turn(conv_id, history, f"Please call the repository '{repo_name}' instead.")
        s = state(conv_id)
        assert s["repository"]["repo_name"] == repo_name and s["repository"]["status"] == store.REPO_PROPOSED

        # 5. Create the repository -> structure proposed and shown
        response = await turn(conv_id, history, "Approved, create the repository.")
        s = state(conv_id)
        repo = s["repository"]
        assert repo["status"] == store.REPO_STRUCTURE_REVIEW and repo["ado_repo_id"] and repo["structure_version"] == 1
        assert live.get_repo(project_name, repo_name).get("defaultBranch") is None    # still empty in ADO
        assert cs.render_structure_for_display(s) in response                        # shown == stored
        v1_paths = {f["path"] for f in repo["structure"]}
        assert any(p.endswith(".go") for p in v1_paths), "structure should follow the Go TRD"

        # 6. Change the structure
        response = await turn(conv_id, history, "Please add a docs/adr folder containing a README.md file.")
        s = state(conv_id)
        repo = s["repository"]
        v2_paths = {f["path"] for f in repo["structure"]}
        assert repo["structure_version"] == 2 and "/docs/adr/README.md" in v2_paths
        assert v1_paths <= v2_paths                                                   # nothing else lost
        assert cs.render_structure_for_display(s) in response

        # 7. Approve -> push exactly the stored structure
        response = await turn(conv_id, history, "Looks good, push it with the commit message 'Initial scaffold'.")
        s = state(conv_id)
        repo = s["repository"]
        assert repo["status"] == store.REPO_PUSHED and s["project"]["workflow_stage"] == store.STAGE_COMPLETE
        commits = live.list_commits(project_name, repo_name, "main")
        assert len(commits) == 1 and commits[0]["comment"].strip() == "Initial scaffold"
        assert commits[0]["commitId"] == repo["commit_id"] and repo["branch"] == "main"
        assert repo["commit_id"] in response or repo["commit_id"][:7] in response

        tree = live.tree_paths(live.list_tree(project_name, repo_name, "main"))
        pushed_files = {p for p, is_folder in tree.items() if not is_folder}
        assert pushed_files == v2_paths                                               # pushed == reviewed
        for f in repo["structure"]:
            assert live.get_file_content(project_name, repo_name, f["path"], "main") == f["content"]

        show(f"\n[evidence] project={project_name} ado_project_id={ado_project_id} repo={repo_name} "
             f"commit={repo['commit_id']} files={len(pushed_files)} structure_version={repo['structure_version']}")
        show(live.render_tree(pushed_files))
    finally:
        if ado_project_id and os.getenv("ADO_E2E_KEEP") != "1":
            assert project_name.startswith("DevFoundry-E2E-")
            show(f"[cleanup] delete ADO project {project_name}: HTTP {live.delete_project(ado_project_id)}")
        live.delete_project_rows(conv_id)
