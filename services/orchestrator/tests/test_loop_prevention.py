import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from services.orchestrator.src.agent.agent_loop import Agent


class SequencedLLMProvider:
    """Returns a predetermined sequence of tool calls to simulate a looping agent."""
    chars_per_token = 4.0

    def __init__(self, responses):
        self.responses = list(responses)
        self.recorded_contexts = []

    def generate(self, context):
        self.recorded_contexts.append(context)
        if self.responses:
            return json.dumps(self.responses.pop(0))
        return json.dumps({
            "reasoning": "Done",
            "tool": "finish_task",
            "tool_args": {"summary": "Completed", "success": True},
        })


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_repeated_file_reads_and_shell_grep(tmp_path: Path):
    target_file = tmp_path / "ChatPanel.tsx"
    target_file.write_text("export const ChatPanel = () => {};\n")

    llm = SequencedLLMProvider([
        # Turn 1: Agent tries to run grep via terminal_proxy -> Should be blocked by Guardrail 1
        {
            "reasoning": "Grep for New Chat",
            "tool": "terminal_proxy",
            "tool_args": {"command": "grep -rn 'New Chat' ChatPanel.tsx"},
        },
        # Turn 2: Agent reads ChatPanel.tsx via file_system (Read #2 of ChatPanel.tsx overall) -> Succeeds with 2nd-read warning
        {
            "reasoning": "Read ChatPanel.tsx properly",
            "tool": "file_system",
            "tool_args": {"action": "read", "path": "ChatPanel.tsx"},
        },
        # Turn 3: Agent tries to read ChatPanel.tsx again (Read #3) -> Blocked by Circuit Breaker!
        {
            "reasoning": "Read ChatPanel.tsx again because I forgot",
            "tool": "file_system",
            "tool_args": {"action": "read", "path": "ChatPanel.tsx"},
        },
        # Turn 4: Finish task
        {
            "reasoning": "Switching approach and finishing",
            "tool": "finish_task",
            "tool_args": {"summary": "Finished without infinite loop", "success": True},
        },
    ])

    config = {"max_iterations": 10, "memory": {"max_tokens": 6000}}
    agent = Agent(llm_provider=llm, config=config, uds_server=None, workspace_root=str(tmp_path))
    agent.dispatcher.execute_async = AsyncMock(return_value="File content: export const ChatPanel...")

    state = await agent.run("Fix New Chat button", auto_approve=True)

    assert state.is_complete is True
    tool_observations = [m.content for m in state.history if m.role.value == "tool"]

    # 1. Verify Turn 1 shell grep was blocked before reaching dispatcher
    assert "Using `terminal_proxy` for `grep`" in tool_observations[0]

    # 2. Verify Turn 2 succeeded but carried the 2nd-time warning
    assert "2nd time reading `ChatPanel.tsx`" in tool_observations[1]

    # 3. Verify Turn 3 was blocked by the circuit breaker
    assert "Error [Circuit Breaker]" in tool_observations[2]

    # Dispatcher should only have been invoked ONCE (Turn 2), never on Turn 1 or Turn 3
    assert agent.dispatcher.execute_async.call_count == 1

    # 4. Verify the Pinned Investigation Ledger was injected into the context on Turn 4
    last_context_user_msg = llm.recorded_contexts[-1][-1]["content"]
    assert "[Pinned Investigation Ledger" in last_context_user_msg
    assert "Inspected `ChatPanel.tsx`" in last_context_user_msg