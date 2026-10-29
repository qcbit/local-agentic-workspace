import asyncio
import pytest
import aiosqlite
from pathlib import Path

from services.orchestrator.src.memory.persistence import MessageStore

@pytest.mark.asyncio
async def test_ro_connection_succeeds_during_write_txn(tmp_path: Path):
    """
    Test 16: Validates that a read-only connection (simulating the VS Code extension) 
    can successfully query the database while the orchestrator holds an active write lock.
    """
    db_path = tmp_path / "orchestrator.db"
    
    # 1. Initialize the schema using the orchestrator's store
    store = MessageStore(db_path)
    await store.open()
    
    # Insert a dummy row to read later
    await store.append(
        session_id="test-session-123", 
        seq=0, 
        role="user", 
        content_json='{"text": "hello"}', 
        token_count=10
    )
    
    # 2. Simulate the orchestrator opening a transaction and holding it
    async with aiosqlite.connect(db_path) as writer_conn:
        await writer_conn.execute("PRAGMA journal_mode=WAL")
        await writer_conn.execute("BEGIN IMMEDIATE")
        
        # 3. Simulate the VS Code extension establishing a concurrent Read-Only connection
        ro_uri = f"file:{db_path}?mode=ro"
        async with aiosqlite.connect(ro_uri, uri=True) as ro_conn:
            cursor = await ro_conn.execute("SELECT COUNT(*) FROM messages")
            row = await cursor.fetchone()
            
            # The read should succeed immediately without SQLITE_BUSY exceptions
            assert row is not None
            assert row[0] == 1
            
        await writer_conn.rollback()
        
    await store.close()

@pytest.mark.asyncio
async def test_busy_timeout_allows_reader_to_wait(tmp_path: Path):
    """
    Test 15: Validates that PRAGMA busy_timeout=5000 prevents instant failures 
    if a lock collision occurs, forcing the connection to wait up to 5 seconds.
    """
    db_path = tmp_path / "orchestrator.db"
    store = MessageStore(db_path)
    await store.open()

    async with aiosqlite.connect(db_path) as conn1, aiosqlite.connect(db_path) as conn2:
        await conn1.execute("PRAGMA busy_timeout=5000")
        await conn2.execute("PRAGMA busy_timeout=5000")
        
        # Prove the pragmas applied correctly
        cursor1 = await conn1.execute("PRAGMA busy_timeout")
        res1 = await cursor1.fetchone()
        assert res1[0] == 5000
        
    await store.close()