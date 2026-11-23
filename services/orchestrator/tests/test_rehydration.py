import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.src.agent.agent_loop import Agent
from services.orchestrator.src.agent.state import AgentState, Message, Role
from services.orchestrator.src.memory.context_manager import SlidingContextManager


# ---------------------------------------------------------------------------
# SlidingContextManager regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_handles_string_and_dict_rows(monkeypatch):
    """Regression: rehydrate() crashed with AttributeError when SQLite rows
    contained a JSON-encoded string instead of a dict."""
    monkeypatch.setenv("ORCH_PERSIST", "1")

    mock_store = MagicMock()
    mock_store.latest = AsyncMock(
        return_value=[
            # Stored by Agent._save_message: json.dumps(str)
            SimpleNamespace(role="user", content=json.dumps("Where is the terminal monitor?")),
            # Stored by SlidingContextManager._persist_message: dict payload
            SimpleNamespace(
                role="tool",
                content=json.dumps({"role": "tool", "content": "Found it", "name": "search_codebase"}),
            ),
        ]
    )
    mock_store.get_meta = AsyncMock(return_value=None)
    mock_store.total_tokens = AsyncMock(return_value=42)

    state = AgentState(user_goal="")
    state.run_id = "test-session-123"
    provider = SimpleNamespace(chars_per_token=3.5, model="qwen2.5:1.5b")

    cm = SlidingContextManager(
        memory_config={"max_tokens": 4000},
        model_name="qwen2.5:1.5b",
        llm_provider=provider,
        state=state,
        message_store=mock_store,
    )

    await cm.rehydrate()

    assert len(state.history) == 2
    assert state.history[0].role == Role.USER
    assert state.history[0].content == "Where is the terminal monitor?"
    assert state.history[0].name is None
    assert state.history[1].role == Role.TOOL
    assert state.history[1].content == "Found it"
    assert state.history[1].name == "search_codebase"
    assert state.total_tokens == 42


def test_build_safe_context_with_pending_task_no_unbound_local():
    """Regression: build_safe_context() raised UnboundLocalError for
    surviving_overflow when _pending_task was already in flight."""
    state = AgentState(user_goal="Find terminal monitor")
    state.run_id = "test-session-456"
    # Populate enough history to force overflow_messages
    for i in range(10):
        state.history.append(Message(role=Role.USER, content=f"Message {i} " + ("x" * 200)))

    provider = SimpleNamespace(chars_per_token=3.5, model="qwen2.5:1.5b")
    cm = SlidingContextManager(
        memory_config={"max_tokens": 100},
        model_name="qwen2.5:1.5b",
        llm_provider=provider,
        state=state,
        max_tokens=100,
    )

    # Simulate an in-flight background compaction task
    in_flight_task = MagicMock()
    in_flight_task.done.return_value = False
    cm._pending_task = in_flight_task

    context = cm.build_safe_context(state, system_prompt="System prompt")

    assert any("[Earlier context summarized asynchronously" in m["content"] for m in context)


# ---------------------------------------------------------------------------
# Agent.reason() & Agent.run() regressions
# ---------------------------------------------------------------------------


def _make_agent(llm_responses):
    """Helper to create an Agent with a deterministic mock LLM provider."""
    provider = MagicMock()
    provider.generate.side_effect = list(llm_responses)
    provider.chars_per_token = 3.5
    provider.model = "mock-model"

    config = {
        "max_iterations": 5,
        "llm": {"model_name": "mock-model"},
        "memory": {"max_tokens": 4000},
        "sandbox": {"strict_mode": True},
    }
    return Agent(llm_provider=provider, config=config, session_id="test-run")


@pytest.mark.asyncio
async def test_reason_raw_decode_handles_trailing_braces():
    """Regression: greedy regex r'\\{.*\\}' failed when trailing commentary
    contained another '}'."""
    raw = (
        '{"reasoning": "Searching", "tool": "search_codebase", "tool_args": {"query": "monitor"}}\n'
        "Note: this will match functions like foo() { return 1; }."
    )
    agent = _make_agent([raw])
    result = await agent.reason([{"role": "user", "content": "hi"}])

    assert result["tool"] == "search_codebase"
    assert result["tool_args"] == {"query": "monitor"}


@pytest.mark.asyncio
async def test_reason_raw_decode_recovers_from_bad_markdown_fence():
    """Regression: malformed fenced block shouldn't mask valid JSON outside it."""
    raw = (
        "```json\n{not valid json}\n```\n"
        '{"reasoning": "Recovered", "tool": "finish_task", "tool_args": {"summary": "done"}}'
    )
    agent = _make_agent([raw])
    result = await agent.reason([{"role": "user", "content": "hi"}])

    assert result["tool"] == "finish_task"
    assert result["tool_args"] == {"summary": "done"}


@pytest.mark.asyncio
async def test_run_retries_once_on_json_parse_error_then_succeeds():
    """Regression: a single JSON parse error should retry once instead of
    immediately aborting the task."""
    agent = _make_agent(
        [
            "Sorry, I forgot to format as JSON.",
            json.dumps(
                {
                    "reasoning": "Fixed formatting",
                    "tool": "finish_task",
                    "tool_args": {"success": True, "summary": "Found terminal monitor."},
                }
            ),
        ]
    )

    state = await agent.run(user_goal="Where is the terminal monitor?")

    assert state.is_complete is True
    assert state.summary == "Found terminal monitor."
    assert any(m.name == "json_parser" and m.is_noise for m in state.history)


@pytest.mark.asyncio
async def test_run_aborts_after_two_consecutive_json_parse_errors():
    """Regression: two consecutive JSON parse errors must abort cleanly
    with a specific summary (not max_iterations)."""
    agent = _make_agent(
        [
            "Not JSON attempt 1",
            "Not JSON attempt 2",
        ]
    )

    state = await agent.run(user_goal="Where is the terminal monitor?")

    assert state.is_complete is True
    assert "after 2 consecutive attempts" in state.summary


@pytest.mark.asyncio
async def test_run_connection_error_does_not_report_max_iterations():
    """Regression: Connection Error must set is_complete=True and an accurate
    summary instead of falling through to the max_iterations message."""
    agent = _make_agent([])
    agent.reason = AsyncMock(
        return_value={
            "reasoning": "Connection Error: Ollama server refused connection",
            "tool": "error",
            "tool_args": {},
        }
    )

    state = await agent.run(user_goal="Where is the terminal monitor?")

    assert state.is_complete is True
    assert state.summary == "Task aborted: The LLM provider is unreachable."