import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.orchestrator.src.agent.agent_loop import Agent
from services.orchestrator.src.tools.registry import ToolRegistry


class SequencedLLMProvider:
    """Returns a predetermined sequence of tool calls to test agent loop guardrails."""
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


@pytest.fixture
def mock_registry():
    mock_uds = MagicMock()
    mock_uds.vector_store = MagicMock()
    mock_uds.vector_store.semantic_search.return_value = [
        {"file_path": "apps/vscode-extension/src/ChatPanel.tsx", "content": "const handleNewChat = () => {}"}
    ]
    mock_uds.message_store = None
    mock_uds.semantic_graph = None
    return ToolRegistry(uds_server=mock_uds, config={})


def test_formatted_tool_descriptions_includes_parameter_schemas(mock_registry: ToolRegistry):
    """Verifies get_formatted_tool_descriptions injects parameter names, types, enums, and optional '?' flags."""
    formatted = mock_registry.get_formatted_tool_descriptions()

    assert "- file_system:" in formatted
    assert '"action": <\'read\'|\'write\'>' in formatted
    assert '"path": <string>' in formatted
    assert '"content"?: <string>' in formatted

    assert "- search_codebase:" in formatted
    assert '"query": <string>' in formatted
    assert '"path_prefix"?: <string>' in formatted


def test_canonicalize_tool_args_maps_common_llm_aliases(mock_registry: ToolRegistry):
    """Verifies LLM parameter hallucinations are normalized to canonical schema keys."""
    # 1. file_system alias: file_path -> path
    fs_args = mock_registry.canonicalize_tool_args(
        "file_system", {"action": "read", "file_path": "src/main.py"}
    )
    assert fs_args == {"action": "read", "path": "src/main.py"}

    # 2. apply_inline_diff aliases: path -> file_path, original_string -> search_string, new_string -> replace_string
    diff_args = mock_registry.canonicalize_tool_args(
        "apply_inline_diff",
        {"path": "src/app.py", "original_string": "old()", "new_string": "new()"},
    )
    assert diff_args == {
        "file_path": "src/app.py",
        "search_string": "old()",
        "replace_string": "new()",
    }

    # 3. search_codebase aliases: pattern -> query, directory -> path_prefix
    search_args = mock_registry.canonicalize_tool_args(
        "search_codebase", {"pattern": "reset_session", "directory": "apps/vscode-extension"}
    )
    assert search_args == {"query": "reset_session", "path_prefix": "apps/vscode-extension"}

    # 4. terminal_proxy alias: cmd -> command
    term_args = mock_registry.canonicalize_tool_args("terminal_proxy", {"cmd": "pytest -v"})
    assert term_args == {"command": "pytest -v"}


def test_validate_tool_args_catches_missing_keys_bad_enums_and_coerces_ints(mock_registry: ToolRegistry):
    """Verifies pre-execution schema validation returns self-healing errors and coerces integer strings."""
    # Missing required 'path'
    err1 = mock_registry.validate_tool_args("file_system", {"action": "read"})
    assert err1 is not None and "Missing required parameter(s) ['path']" in err1

    # Invalid enum value on 'action'
    err2 = mock_registry.validate_tool_args("file_system", {"action": "delete", "path": "foo.py"})
    assert err2 is not None and "Invalid value 'delete'" in err2

    # Missing 'content' when action == 'write'
    err3 = mock_registry.validate_tool_args("file_system", {"action": "write", "path": "foo.py"})
    assert err3 is not None and "Parameter 'content' is required" in err3

    # Coerces string integer "42" -> 42 on extract_code_structure
    ast_args = {"file_path": "foo.py", "line_number": "42"}
    err4 = mock_registry.validate_tool_args("extract_code_structure", ast_args)
    assert err4 is None
    assert ast_args["line_number"] == 42


@pytest.mark.asyncio
async def test_execute_tool_async_search_codebase_forwards_path_prefix(mock_registry: ToolRegistry):
    """Verifies execute_tool_async routes search_codebase to _search_codebase_async with path_prefix."""
    obs = await mock_registry.execute_tool_async(
        "search_codebase", {"query": "New Chat", "directory": "apps/vscode-extension", "limit": 3}
    )
    assert "Codebase Search Results:" in obs
    assert "ChatPanel.tsx" in obs
    mock_registry.vector_store.semantic_search.assert_called_once_with(
        query="New Chat", limit=3, path_prefix="apps/vscode-extension"
    )


@pytest.mark.asyncio
async def test_agent_loop_canonicalization_validation_and_circuit_breakers(tmp_path: Path):
    """End-to-end test of Agent.run() with alias normalization, schema validation, shell block, and 3rd-read circuit breaker."""
    target_file = tmp_path / "ChatPanel.tsx"
    target_file.write_text("export const ChatPanel = () => {};\n")

    llm = SequencedLLMProvider([
        # Turn 1: Missing required argument -> Caught by validate_tool_args
        {
            "reasoning": "Forgot path argument",
            "tool": "file_system",
            "tool_args": {"action": "read"},
        },
        # Turn 2: Forbidden shell grep -> Caught by Guardrail 1 (does NOT increment file read count)
        {
            "reasoning": "Try grep in shell",
            "tool": "terminal_proxy",
            "tool_args": {"command": "grep -rn 'New Chat' ChatPanel.tsx"},
        },
        # Turn 3: Read #1 using 'file_path' alias -> Canonicalized to 'path' & succeeds
        {
            "reasoning": "Read ChatPanel.tsx (1st time)",
            "tool": "file_system",
            "tool_args": {"action": "read", "file_path": "ChatPanel.tsx"},
        },
        # Turn 4: Read #2 using canonical 'path' -> Succeeds with 2nd-read warning
        {
            "reasoning": "Read ChatPanel.tsx (2nd time)",
            "tool": "file_system",
            "tool_args": {"action": "read", "path": "ChatPanel.tsx"},
        },
        # Turn 5: Read #3 -> Blocked by Circuit Breaker!
        {
            "reasoning": "Read ChatPanel.tsx (3rd time)",
            "tool": "file_system",
            "tool_args": {"action": "read", "path": "ChatPanel.tsx"},
        },
        # Turn 6: Complete task
        {
            "reasoning": "All guardrails verified",
            "tool": "finish_task",
            "tool_args": {"summary": "Guardrails passed!", "success": True},
        },
    ])

    mock_uds = MagicMock()
    mock_uds.vector_store = MagicMock()
    mock_uds.message_store = None
    mock_uds.semantic_graph = None
    mock_uds.send_notification = AsyncMock()

    config = {"max_iterations": 10, "memory": {"max_tokens": 6000}}
    agent = Agent(llm_provider=llm, config=config, uds_server=mock_uds, workspace_root=str(tmp_path))
    agent.dispatcher.execute_async = AsyncMock(return_value="File content: export const ChatPanel...")

    state = await agent.run("Test all harness guardrails", auto_approve=True)

    assert state.is_complete is True
    tool_obs = [m.content for m in state.history if m.role.value == "tool"]

    # Turn 1: Schema validation error
    assert "Error [Invalid Tool Arguments]" in tool_obs[0]
    # Turn 2: Shell grep blocked
    assert "Using `terminal_proxy` for `grep`" in tool_obs[1]
    # Turn 3: 1st valid file read
    assert tool_obs[2] == "File content: export const ChatPanel..."
    # Turn 4: 2nd valid file read carries harness warning
    assert "2nd time reading `ChatPanel.tsx`" in tool_obs[3]
    # Turn 5: 3rd file read blocked by circuit breaker
    assert "Error [Circuit Breaker]" in tool_obs[4]

    # Dispatcher was only called twice (Turns 3 and 4)
    assert agent.dispatcher.execute_async.call_count == 2