import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.orchestrator.src.agent.agent_loop import Agent
from services.orchestrator.src.agent.state import AgentState, Message, Role
from services.orchestrator.src.ipc.uds_server import JsonRpcUdsServer
from services.orchestrator.src.memory.persistence import MessageStore


class DummyLLMProvider:
    chars_per_token = 4.0

    def generate(self, context):
        return json.dumps({
            "reasoning": "Completing the task immediately.",
            "tool": "finish_task",
            "tool_args": {"summary": "Done!", "success": True}
        })


@pytest.fixture
async def uds_server_with_db(tmp_path: Path):
    """Creates a JsonRpcUdsServer backed by an isolated temporary workspace and SQLite DB."""
    with patch("services.orchestrator.src.ipc.uds_server.LocalVectorStore") as MockVectorStore:
        MockVectorStore.return_value = MagicMock()
        server = JsonRpcUdsServer(workspace_root=str(tmp_path), host="127.0.0.1", port=0)
        await server.message_store.open()
        yield server
        await server.message_store.close()


@pytest.mark.asyncio
async def test_memory_state_reference_stays_synced_on_new_session(uds_server_with_db, tmp_path: Path):
    """Bug #1: Verify self.memory.state points to the new AgentState after Agent.run() resets state."""
    config = {"max_iterations": 5, "memory": {"max_tokens": 6000}}
    agent = Agent(
        llm_provider=DummyLLMProvider(),
        config=config,
        uds_server=uds_server_with_db,
        workspace_root=str(tmp_path),
    )

    initial_state_id = id(agent.state)
    assert agent.memory.state is agent.state

    # Run a fresh task (non-followup creates a new AgentState instance)
    final_state = await agent.run(user_goal="Test state sync", auto_approve=True)

    assert id(final_state) != initial_state_id
    assert agent.state is final_state
    # CRITICAL: SlidingContextManager must reference the active AgentState, not the stale __init__ one
    assert agent.memory.state is agent.state
    assert agent.memory.state.run_id == final_state.run_id


@pytest.mark.asyncio
async def test_no_duplicate_sqlite_rows_on_ui_history_rehydration(uds_server_with_db, tmp_path: Path):
    """Bug #3: Verify passing history_payload for an already-persisted session does not duplicate rows."""
    store: MessageStore = uds_server_with_db.message_store
    session_id = "test-session-123"

    # Seed SQLite with an existing 2-message trajectory
    await store.append(session_id, 0, "user", json.dumps("Hello agent"), 5)
    await store.append(session_id, 1, "assistant", json.dumps({"reasoning": "Hi", "tool": "finish_task", "tool_args": {"summary": "Hello!"}}), 10)

    config = {"max_iterations": 5, "memory": {"max_tokens": 6000}}
    agent = Agent(
        llm_provider=DummyLLMProvider(),
        config=config,
        uds_server=uds_server_with_db,
        workspace_root=str(tmp_path),
    )
    agent.state.run_id = session_id

    # Simulate a case where memory.rehydrate() is a no-op (e.g., ORCH_PERSIST=0 or cold start)
    # and the UI sends history_payload alongside a follow-up goal
    agent.memory.rehydrate = AsyncMock()

    ui_history_payload = [
        {"role": "user", "content": "Hello agent"},
        {"role": "agent", "content": "Hello!"},
    ]

    await agent.run(
        user_goal="Follow-up question",
        auto_approve=True,
        history_payload=ui_history_payload,
    )

    # Fetch all messages for this session from SQLite
    db_path = tmp_path / ".agentic_workspace" / "db" / "orchestrator.db"
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()

    # Original 2 rows + 3 new rows from follow-up turn (user, assistant, tool finish_task) = 5 rows.
    # Without the fix, the 2 UI history messages would be re-inserted, resulting in 7 rows.
    user_messages = [r for r in rows if r[0] == "user" and "Hello agent" in r[1]]
    assert len(user_messages) == 1, f"Expected 'Hello agent' once, found {len(user_messages)} times"


@pytest.mark.asyncio
async def test_uds_history_lifecycle_and_active_session_deletion(uds_server_with_db, tmp_path: Path):
    """Bugs #6 & #7: Validate load_history_list, fetch_history, and delete_history cleanup."""
    server: JsonRpcUdsServer = uds_server_with_db
    store: MessageStore = server.message_store
    session_id = "session-to-delete-999"

    # 1. Seed messages and context_meta
    await store.append(session_id, 0, "user", json.dumps({"text": "Fix the login button"}), 6)
    await store.append(session_id, 1, "assistant", json.dumps({"reasoning": "Checking auth", "tool": "finish_task"}), 12)
    await store.set_meta(session_id, "summary", json.dumps("User asked to fix login"))

    # Simulate an active persistent_agent bound to this session
    server.persistent_agent = MagicMock()
    server.persistent_agent.state.run_id = session_id

    # 2. Test load_history_list
    load_req = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "load_history_list", "params": {"query": "login"}})
    load_res = await server.process_request(load_req)
    sessions = load_res["result"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert "Fix the login button" in sessions[0]["title"]
    assert sessions[0]["timestamp"] > 0

    # 3. Test fetch_history
    fetch_req = json.dumps({"jsonrpc": "2.0", "id": "2", "method": "fetch_history", "params": {"session_id": session_id}})
    fetch_res = await server.process_request(fetch_req)
    history_rows = fetch_res["result"]["history"]
    assert len(history_rows) == 2
    assert history_rows[0]["role"] == "user"

    # 4. Test delete_history clears messages, context_meta, AND resets active persistent_agent
    del_req = json.dumps({"jsonrpc": "2.0", "id": "3", "method": "delete_history", "params": {"session_id": session_id}})
    del_res = await server.process_request(del_req)
    assert del_res["result"]["status"] == "success"

    # Verify persistent_agent was wiped so ghost session cannot continue
    assert server.persistent_agent is None

    # Verify both tables are empty for session_id
    assert await store.max_seq(session_id) is None
    assert await store.get_meta(session_id, "summary") is None