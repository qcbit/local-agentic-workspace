"""Unit tests for Stale Observation Trimming in SlidingContextManager.build_safe_context.

Verifies that:
- Recent tool observations (within retain_recent_observations) are preserved verbatim.
- Older tool observations are replaced with a compact stub string.
- User and Assistant messages are never modified.
- state.history is never mutated (purity invariant).
- The status heuristic correctly identifies errors.
- Token savings are positive when trimming occurs.
"""

import sys
import os
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from services.orchestrator.src.agent.state import Role, Message, State
from services.orchestrator.src.memory.context_manager import SlidingContextManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_message(role: Role, content: str, name: str | None = None) -> Message:
    """Helper to build a Message instance."""
    if name is not None:
        return Message(role=role, name=name, content=content)
    return Message(role=role, name=None, content=content)


def _make_state(history: list[Message], user_goal: str = "Implement a feature") -> State:
    """Helper to build a State instance."""
    state = State(user_goal=user_goal, summary=None)
    state.history = history
    return state


def _make_manager(retain_recent_observations: int = 2, max_tokens: int = 50000) -> SlidingContextManager:
    """Helper to build a SlidingContextManager with a mock provider."""
    mock_provider = SimpleNamespace(
        chars_per_token=4.0,
        model="test-model",
    )
    memory_config = {
        "max_tokens": max_tokens,
        "retain_recent_observations": retain_recent_observations,
    }
    return SlidingContextManager(
        memory_config=memory_config,
        model_name="test-model",
        llm_provider=mock_provider,
    )


def _build_history(num_tool_msgs: int, tool_content: str = "x" * 2000) -> list[Message]:
    """Build a history with alternating assistant and tool messages.

    Oldest first (index 0 = oldest). The last (newest) messages are at the end.
    """
    msgs: list[Message] = []
    for i in range(num_tool_msgs):
        # Assistant reasoning before each tool call
        msgs.append(_make_message(Role.ASSISTANT, f"Reasoning turn {i}: let me check something."))
        # Tool observation
        msgs.append(_make_message(Role.TOOL, tool_content, name="terminal_proxy"))
    return msgs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStaleObservationTrim:
    """Verify the stale observation trimming behavior in build_safe_context."""

    def test_recent_tools_preserved_verbatim(self):
        """The last N tool observations (N = retain_recent_observations) keep their raw content."""
        retain = 2
        manager = _make_manager(retain_recent_observations=retain)
        tool_content = "Output of a long command that produced lots of text. " * 50
        history = _build_history(num_tool_msgs=5, tool_content=tool_content)
        state = _make_state(history)

        result = manager.build_safe_context(state, "You are a helpful agent.")

        # Count tool-stub messages in the result
        stub_msgs = [m for m in result if "[Stale Observation Trimmed:" in m["content"]]
        raw_tool_msgs = [
            m for m in result
            if m["role"] == "user" and "Observation:" in m["content"] and "[Stale" not in m["content"]
        ]

        # All 5 tool messages should be retained (within 50k token budget)
        # Only the 3 oldest should be stubbed; 2 newest preserved
        assert len(raw_tool_msgs) == retain, (
            f"Expected {retain} raw tool messages, got {len(raw_tool_msgs)}"
        )
        assert len(stub_msgs) == 5 - retain, (
            f"Expected {5 - retain} stub messages, got {len(stub_msgs)}"
        )

    def test_stale_tools_stubbed(self):
        """Older tool messages contain the stub format with tool name."""
        manager = _make_manager(retain_recent_observations=2)
        tool_content = "A" * 5000
        history = _build_history(num_tool_msgs=4, tool_content=tool_content)
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub_msgs = [m for m in result if "[Stale Observation Trimmed:" in m["content"]]
        for m in stub_msgs:
            assert "terminal_proxy" in m["content"]
            assert "status=" in m["content"]
            assert "length=" in m["content"]
            # The raw content should NOT appear in the stub
            assert tool_content not in m["content"]

    def test_status_heuristic_error(self):
        """A stale tool message whose content starts with 'Error:' gets status=error in the stub."""
        manager = _make_manager(retain_recent_observations=0)  # trim ALL
        history = [
            _make_message(Role.ASSISTANT, "Let me run the test."),
            _make_message(Role.TOOL, "Error: test failed with exit code 1\nTraceback...", name="terminal_proxy"),
        ]
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub = [m for m in result if "[Stale Observation Trimmed:" in m["content"]]
        assert len(stub) == 1
        assert "status=error" in stub[0]["content"]

    def test_status_heuristic_ok(self):
        """A stale tool message with normal output gets status=ok in the stub."""
        manager = _make_manager(retain_recent_observations=0)  # trim ALL
        history = [
            _make_message(Role.ASSISTANT, "Reading a file."),
            _make_message(Role.TOOL, "def hello():\n    print('world')", name="file_system"),
        ]
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub = [m for m in result if "[Stale Observation Trimmed:" in m["content"]]
        assert len(stub) == 1
        assert "status=ok" in stub[0]["content"]

    def test_length_preserved_in_stub(self):
        """The stub contains the original content length."""
        manager = _make_manager(retain_recent_observations=0)
        original = "Z" * 3456
        history = [
            _make_message(Role.ASSISTANT, "Running a command."),
            _make_message(Role.TOOL, original, name="terminal_proxy"),
        ]
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub = [m for m in result if "[Stale Observation Trimmed:" in m["content"]][0]
        assert f"length={len(original)}" in stub["content"]

    def test_user_and_assistant_messages_untouched(self):
        """All Role.USER and Role.ASSISTANT messages are byte-identical in the output."""
        manager = _make_manager(retain_recent_observations=0)  # max trimming
        assistant_text = "I will analyze the codebase and implement the fix."
        history = [
            _make_message(Role.ASSISTANT, assistant_text),
            _make_message(Role.TOOL, "W" * 5000, name="terminal_proxy"),
            _make_message(Role.ASSISTANT, assistant_text),
            _make_message(Role.TOOL, "W" * 5000, name="file_system"),
        ]
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        # user_goal is pinned as a user message
        user_msgs = [m for m in result if m["role"] == "user" and m["content"] == state.user_goal]
        assert len(user_msgs) == 1

        # Assistant messages should appear exactly as written
        assistant_in_result = [m for m in result if m["role"] == "assistant"]
        for msg in assistant_in_result:
            assert msg["content"] == assistant_text

    def test_history_not_mutated(self):
        """state.history is unchanged after build_safe_context (deep-equality on each Message)."""
        manager = _make_manager(retain_recent_observations=1)
        tool_content = "Q" * 3000
        history = _build_history(num_tool_msgs=4, tool_content=tool_content)
        # Snapshot the history before
        snapshot = [(m.role, m.name, m.content) for m in history]

        state = _make_state(history)
        manager.build_safe_context(state, "System prompt.")

        # Verify history is unchanged
        assert len(state.history) == len(history)
        for i, (role, name, content) in enumerate(snapshot):
            assert state.history[i].role == role
            assert state.history[i].name == name
            assert state.history[i].content == content

    def test_retain_zero_trims_all(self):
        """With retain_recent_observations=0, every tool message is stubbed."""
        manager = _make_manager(retain_recent_observations=0)
        history = _build_history(num_tool_msgs=3, tool_content="V" * 2000)
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub_count = sum(1 for m in result if "[Stale Observation Trimmed:" in m["content"])
        raw_count = sum(
            1 for m in result
            if m["role"] == "user" and "Observation:" in m["content"] and "[Stale" not in m["content"]
        )
        assert stub_count == 3
        assert raw_count == 0

    def test_retain_large_disables_trim(self):
        """With retain_recent_observations=999, all tool messages are verbatim."""
        manager = _make_manager(retain_recent_observations=999)
        history = _build_history(num_tool_msgs=3, tool_content="B" * 2000)
        state = _make_state(history)

        result = manager.build_safe_context(state, "System prompt.")

        stub_count = sum(1 for m in result if "[Stale Observation Trimmed:" in m["content"])
        raw_count = sum(
            1 for m in result
            if m["role"] == "user" and "Observation:" in m["content"] and "[Stale" not in m["content"]
        )
        assert stub_count == 0
        assert raw_count == 3

    def test_token_savings_positive(self):
        """When trimming occurs, the estimated token count should be lower than untrimmed."""
        tool_content = "T" * 10000  # large content
        history = _build_history(num_tool_msgs=5, tool_content=tool_content)
        state = _make_state(history)

        # With trimming (retain=2)
        manager_trim = _make_manager(retain_recent_observations=2)
        result_trim = manager_trim.build_safe_context(state, "System prompt.")

        # Without trimming (retain=999)
        manager_no_trim = _make_manager(retain_recent_observations=999)
        result_no_trim = manager_no_trim.build_safe_context(state, "System prompt.")

        tokens_trim = sum(manager_trim._estimate_tokens(m["content"]) for m in result_trim)
        tokens_no_trim = sum(manager_no_trim._estimate_tokens(m["content"]) for m in result_no_trim)

        assert tokens_trim < tokens_no_trim
