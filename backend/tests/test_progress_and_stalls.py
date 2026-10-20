"""Stall detection for streamed answers and the live progress shown while a chat turn runs."""
import asyncio
import time

import pytest

from app.services import llm_client as lc
from app.services import progress


class FakeStream:
    """An Agent Framework-like ResponseStream: async iteration of updates, then get_final_response()."""

    def __init__(self, delays, final="the TRD"):
        self.delays, self.final, self.closed, self.index = list(delays), final, False, 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.delays):
            raise StopAsyncIteration
        await asyncio.sleep(self.delays[self.index])
        self.index += 1
        return f"chunk {self.index}"

    async def get_final_response(self):
        return self.final

    async def close(self):
        self.closed = True


class Provider:
    provider_id, name = "maq_ai", "MAQ AI (SLM)"


@pytest.fixture
def fast_limits(monkeypatch):
    monkeypatch.setenv("STREAM_STALL_SECONDS", "10")       # minimum allowed; tests patch the helper for speed
    monkeypatch.setattr(lc, "stream_stall_seconds", lambda: 0.2)
    monkeypatch.setattr(lc, "provider_total_seconds", lambda provider_id: 1.0)


def test_a_normal_stream_is_read_to_the_end(fast_limits):
    stream = FakeStream([0.01] * 5)
    assert asyncio.run(lc.read_stream_with_watchdog(stream, Provider())) == "the TRD"
    assert not stream.closed


def test_a_stalled_stream_is_abandoned(fast_limits):
    # seen live: a MAQ AI answer kept the request open for 27 minutes
    stream = FakeStream([0.01, 0.01, 5.0])
    started = time.time()
    with pytest.raises(lc.StreamStalledError, match="no new text"):
        asyncio.run(lc.read_stream_with_watchdog(stream, Provider()))
    assert time.time() - started < 1.0 and stream.closed


def test_a_slow_but_steady_stream_is_cut_at_the_total_limit(fast_limits):
    stream = FakeStream([0.15] * 20)                        # each chunk in time, the whole answer too slow
    with pytest.raises(lc.StreamStalledError, match="within"):
        asyncio.run(lc.read_stream_with_watchdog(stream, Provider()))
    assert stream.closed


def test_stall_limits_come_from_settings(monkeypatch):
    monkeypatch.setenv("STREAM_STALL_SECONDS", "90")
    monkeypatch.setenv("MAQ_AI_TIMEOUT_SECONDS", "300")
    assert lc.stream_stall_seconds() == 90 and lc.provider_total_seconds("maq_ai") == 300
    monkeypatch.delenv("STREAM_STALL_SECONDS")
    monkeypatch.delenv("MAQ_AI_TIMEOUT_SECONDS")
    assert lc.stream_stall_seconds() == 120 and lc.provider_total_seconds("maq_ai") == 600


def test_a_stall_moves_on_to_the_next_provider(fast_limits):
    from agent_framework import Message

    class StreamingClient:
        calls = 0

        def get_response(self, stream=False, **kwargs):
            StreamingClient.calls += 1
            return FakeStream([0.01, 5.0])                  # stalls after the first chunk

    class Answering:
        calls = 0

        async def _answer(self):
            return "groq-answer"

        def get_response(self, stream=False, **kwargs):
            Answering.calls += 1
            return self._answer()

    maq = lc.ProviderSpec("maq_ai", "MAQ AI (SLM)", StreamingClient(), "qwen")
    groq = lc.ProviderSpec("groq", "Groq", Answering(), "gpt-oss-20b")
    client = lc.FallbackChatCompletionClient([maq, groq])
    result = asyncio.run(client.get_response([Message(role="user", contents=["write the TRD"])]))
    assert result == "groq-answer" and StreamingClient.calls == 1 and Answering.calls == 1


def test_progress_is_tracked_per_conversation_and_owner():
    progress.start("conv-1", "user-a")
    progress.step("Writing the TRD", "conv-1")
    progress.detail("Using MAQ AI (SLM)", "conv-1")
    current = progress.get("conv-1", "user-a")
    assert current["step"] == "Writing the TRD" and current["detail"] == "Using MAQ AI (SLM)"
    assert progress.get("conv-1", "someone-else") is None              # only the owner sees it
    progress.step("Preparing the reply", "conv-1")
    assert progress.get("conv-1", "user-a")["detail"] == ""             # a new step clears the detail
    progress.finish("conv-1")
    assert progress.get("conv-1", "user-a") is None


def test_progress_follows_the_running_request_without_ids():
    async def turn():
        progress.start("conv-2", "u")
        progress.step("Deciding the next step")                         # no id: taken from the request context
        await asyncio.to_thread(progress.detail, "Running `npx create-vite@latest`")   # generator in a worker thread
        return progress.get("conv-2", "u")

    current = asyncio.run(turn())
    assert current["step"] == "Deciding the next step" and current["detail"].startswith("Running `npx")
    progress.finish("conv-2")


# ---------------------------------------------------------------------------- Stop (POST /api/chat/cancel/{id})

def _fake_turn(monkeypatch, body):
    """Replaces the chat turn with `body` (an async function of the turn's log list)."""
    from app.services import conversation_service as cs
    log = []

    async def fake(conversation_id, user_query, file=None, *, user_id):
        await body(log)
        log.append("saved")                       # the real turn saves the history only at the very end
        return cs.ChatResponse(conversation_id=conversation_id, response="done")

    monkeypatch.setattr(cs, "_process_chat_request", fake)
    return cs, log


def test_stop_cancels_a_turn_before_any_azure_devops_action(monkeypatch):
    async def body(log):
        log.append("thinking")
        await asyncio.sleep(5)                     # e.g. waiting for the model
        log.append("model answered")

    cs, log = _fake_turn(monkeypatch, body)

    async def scenario():
        turn = asyncio.create_task(cs.process_chat_request("conv-stop", "write the TRD", user_id="u"))
        await asyncio.sleep(0.05)
        assert progress.cancel("conv-stop", "someone-else") == {"status": "idle"}     # only the owner can stop it
        assert progress.cancel("conv-stop", "u") == {"status": "cancelled"}
        return await asyncio.wait_for(turn, 2)

    started = time.time()
    result = asyncio.run(scenario())
    assert time.time() - started < 2                                             # did not wait for the model
    assert result.response == cs.STOPPED_MESSAGE and log == ["thinking"]         # nothing saved
    assert progress.get("conv-stop", "u") is None and progress.cancel("conv-stop", "u") == {"status": "idle"}


def test_stop_after_an_azure_devops_action_started_lets_the_turn_finish(monkeypatch):
    async def body(log):
        progress.begin_side_effect("Creating the repository in Azure DevOps")
        await asyncio.sleep(0.2)                   # the action and the rest of the turn
        log.append("repository created")

    cs, log = _fake_turn(monkeypatch, body)

    async def scenario():
        turn = asyncio.create_task(cs.process_chat_request("conv-busy", "create the repository", user_id="u"))
        await asyncio.sleep(0.05)
        answer = progress.cancel("conv-busy", "u")
        return answer, await turn

    answer, result = asyncio.run(scenario())
    assert answer == {"status": "finishing", "action": "Creating the repository in Azure DevOps"}
    assert result.response == "done" and log == ["repository created", "saved"]


def test_a_stop_that_came_first_prevents_the_action(monkeypatch):
    def tool_in_a_worker_thread(log):
        progress.begin_side_effect("Pushing to Azure DevOps")      # raises: Stop was clicked before
        log.append("PUSHED")

    async def body(log):
        await asyncio.sleep(0.1)
        await asyncio.to_thread(tool_in_a_worker_thread, log)

    cs, log = _fake_turn(monkeypatch, body)

    async def scenario():
        turn = asyncio.create_task(cs.process_chat_request("conv-race", "push it", user_id="u"))
        await asyncio.sleep(0.02)
        entry = progress._progress["conv-race"]
        entry["cancel_requested"] = True          # Stop arrived, the task is not cancelled yet (race)
        return await turn

    result = asyncio.run(scenario())
    assert result.response == cs.STOPPED_MESSAGE and "PUSHED" not in log


def test_the_cancel_route_and_side_effects_are_wired():
    import inspect
    from app.api.routes import conversations
    from app.services import backlog_service, pr_review_service
    from app.services.agents import repo_setup_agent
    assert "progress.cancel(conversation_id, user.user_id)" in inspect.getsource(conversations.cancel_chat)
    source = inspect.getsource(repo_setup_agent)
    for action in ("Creating the Azure DevOps project", "Updating the Azure DevOps project",
                   "Creating the repository in Azure DevOps", "Pushing to Azure DevOps"):
        assert f'progress.begin_side_effect("{action}")' in source
    assert 'begin_side_effect("Creating the work items in Azure Boards")' in inspect.getsource(backlog_service)
    assert "begin_side_effect(f\"Posting the review comments" in inspect.getsource(pr_review_service.post_review)


def test_the_pipeline_reports_its_steps():
    import inspect
    from app.services import conversation_service as cs
    source = inspect.getsource(cs._execute_agent_pipeline)
    for marker in ('progress.step("Deciding the next step")', "OPERATION_STEPS.get(operation", 'progress.step("Preparing the reply")'):
        assert marker in source
    assert cs.OPERATION_STEPS["GENERATE_TRD"] == "Writing the TRD"


# ---------------------------------------------------------------------------- tools inside a model turn (AGT-28/34)

def test_tool_time_is_neither_a_stall_nor_part_of_the_total(fast_limits):
    # seen live: 166 s of generator commands inside the Repo Setup agent's turn were taken for a stalled model
    stream = FakeStream([0.01, 0.6, 0.01])        # 0.6 s gap > stall 0.2 s, but a tool runs meanwhile

    async def scenario():
        progress.start("conv-tool", "u")

        async def tool_runs_during_the_gap():
            await asyncio.sleep(0.02)
            progress.tool_started()
            await asyncio.sleep(0.55)
            progress.tool_finished()

        asyncio.ensure_future(tool_runs_during_the_gap())
        try:
            return await lc.read_stream_with_watchdog(stream, Provider())
        finally:
            progress.finish("conv-tool")

    assert asyncio.run(scenario()) == "the TRD" and not stream.closed


def test_a_real_stall_is_still_caught_after_a_tool(fast_limits):
    stream = FakeStream([0.01, 5.0])

    async def scenario():
        progress.start("conv-stall", "u")
        try:
            return await lc.read_stream_with_watchdog(stream, Provider())
        finally:
            progress.finish("conv-stall")

    with pytest.raises(lc.StreamStalledError, match="no new text"):
        asyncio.run(scenario())


def test_wait_for_tools_returns_when_the_thread_is_done():
    async def scenario():
        progress.start("conv-wait", "u")
        progress.tool_started()

        def generator_thread():
            time.sleep(0.3)
            progress.tool_finished()

        worker = asyncio.ensure_future(asyncio.to_thread(generator_thread))
        finished = await progress.wait_for_tools("conv-wait", timeout=5)
        await worker
        progress.finish("conv-wait")
        return finished

    assert asyncio.run(scenario()) is True


def test_a_once_per_turn_tool_runs_once_and_tools_are_tracked():
    from types import SimpleNamespace
    from app.services.agents.repo_setup_agent import tool_run_middleware
    middleware = tool_run_middleware()
    pushes, seen_running = [], []

    async def scenario():
        progress.start("conv-mw", "u")
        results = []
        for _ in range(2):
            context = SimpleNamespace(function=SimpleNamespace(name="push_repository_structure"), result=None)

            async def call_next(context=context):
                seen_running.append(progress.tools_running())
                pushes.append(1)
                context.result = {"status": "success", "commit_id": "abc"}

            await middleware(context, call_next)
            results.append(context.result)
        progress.finish("conv-mw")
        return results

    results = asyncio.run(scenario())
    assert len(pushes) == 1 and seen_running == [1]               # second call returned the first result
    assert results[0] == results[1] == {"status": "success", "commit_id": "abc"}


def test_a_failed_once_per_turn_tool_may_be_retried():
    from types import SimpleNamespace
    from app.services.agents.repo_setup_agent import tool_run_middleware
    middleware, calls = tool_run_middleware(), []

    async def scenario():
        for status in ("error", "success"):
            context = SimpleNamespace(function=SimpleNamespace(name="push_repository_structure"), result=None)

            async def call_next(context=context, status=status):
                calls.append(status)
                context.result = {"status": status}

            await middleware(context, call_next)

    asyncio.run(scenario())
    assert calls == ["error", "success"]


def test_the_bubble_says_when_a_backup_model_answers():
    from types import SimpleNamespace
    first = SimpleNamespace(name="Azure OpenAI", stream=False)
    maq = SimpleNamespace(name="MAQ AI (SLM)", stream=True)
    groq = SimpleNamespace(name="Groq (gpt-oss-20b)", stream=False)
    assert lc.backup_notice(first, []) == "Using Azure OpenAI"
    assert lc.backup_notice(maq, ["[Azure OpenAI]: 403"]) == (
        "The main model is busy, so a backup (MAQ AI (SLM)) is answering; this can take a few minutes")
    assert lc.backup_notice(groq, ["[Azure OpenAI]: 403"]).endswith("is answering")


def _clients(first_streams):
    from agent_framework import Message  # noqa: F401

    class StreamingClient:
        calls = 0

        def get_response(self, stream=False, **kwargs):
            StreamingClient.calls += 1
            return FakeStream(first_streams[min(StreamingClient.calls, len(first_streams)) - 1])

    class Answering:
        calls = 0

        async def _answer(self):
            return "groq-answer"

        def get_response(self, stream=False, **kwargs):
            Answering.calls += 1
            return self._answer()
    return StreamingClient, Answering


def test_a_stall_before_the_first_token_retries_the_same_provider_once(fast_limits):
    # seen live (ref eeabebc5): MAQ AI sent nothing for 120 s, then answered the identical retry
    from agent_framework import Message
    StreamingClient, Answering = _clients([[5.0], [0.01, 0.01]])
    maq = lc.ProviderSpec("maq_ai", "MAQ AI (SLM)", StreamingClient(), "qwen")
    groq = lc.ProviderSpec("groq", "Groq", Answering(), "gpt-oss-20b")
    result = asyncio.run(lc.FallbackChatCompletionClient([maq, groq]).get_response(
        [Message(role="user", contents=["add a docs folder"])]))
    assert result == "the TRD" and StreamingClient.calls == 2 and Answering.calls == 0


def test_two_stalls_before_the_first_token_move_on(fast_limits):
    from agent_framework import Message
    StreamingClient, Answering = _clients([[5.0], [5.0]])
    maq = lc.ProviderSpec("maq_ai", "MAQ AI (SLM)", StreamingClient(), "qwen")
    groq = lc.ProviderSpec("groq", "Groq", Answering(), "gpt-oss-20b")
    result = asyncio.run(lc.FallbackChatCompletionClient([maq, groq]).get_response(
        [Message(role="user", contents=["add a docs folder"])]))
    assert result == "groq-answer" and StreamingClient.calls == 2 and Answering.calls == 1
