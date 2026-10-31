import asyncio
import pytest
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone

from services.orchestrator.src.memory.persistence import MessageStore

@pytest.mark.asyncio
async def test_snapshot_backup(tmp_path: Path):
    """Validates the VACUUM INTO snapshot capability."""
    active_db = tmp_path / "active.db"
    backup_db = tmp_path / "backup.db"
    
    store = MessageStore(active_db)
    await store.open()
    
    # Insert dummy data
    await store.append("session-1", 0, "user", '{"text": "hello"}', 10)
    
    # Take snapshot
    await store.snapshot_backup(backup_db)
    await store.close()
    
    # Verify backup exists and contains the data
    assert backup_db.exists()
    async with aiosqlite.connect(backup_db) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM messages")
        row = await cursor.fetchone()
        assert row[0] == 1

@pytest.mark.asyncio
async def test_archive_inactive_sessions(tmp_path: Path):
    """Validates cross-database archival using ATTACH DATABASE."""
    active_db = tmp_path / "active.db"
    archive_db = tmp_path / "archive.db"
    
    store = MessageStore(active_db)
    await store.open()
    
    # 1. Insert an ACTIVE session (created just now)
    await store.append("active-session", 0, "user", '{"text": "recent"}', 5)
    
    # 2. Insert a STALE session (manually backdate the created_at timestamp)
    await store.append("stale-session", 0, "user", '{"text": "old"}', 5)
    
    # Manually backdate "stale-session" to 20 days ago
    assert store._conn is not None
    past_date = (datetime.now(timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%fZ')
    await store._conn.execute(
        "UPDATE messages SET created_at = ? WHERE session_id = ?", 
        (past_date, "stale-session")
    )
    await store._conn.commit()
    
    # 3. Run Archival Sweep (Retention: 14 days)
    archived_count = await store.archive_inactive_sessions(retention_days=14, archive_path=archive_db)
    
    # Only the stale session should have been moved
    assert archived_count == 1
    
    # 4. Verify Active DB (should only contain 'active-session')
    async with aiosqlite.connect(active_db) as conn:
        cursor = await conn.execute("SELECT session_id FROM messages")
        active_rows = await cursor.fetchall()
        assert len(active_rows) == 1
        assert active_rows[0][0] == "active-session"
        
    # 5. Verify Archive DB (should only contain 'stale-session')
    assert archive_db.exists()
    async with aiosqlite.connect(archive_db) as conn:
        cursor = await conn.execute("SELECT session_id FROM messages")
        archive_rows = await cursor.fetchall()
        assert len(archive_rows) == 1
        assert archive_rows[0][0] == "stale-session"

    await store.close()