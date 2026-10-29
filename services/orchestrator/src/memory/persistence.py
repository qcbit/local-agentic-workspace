import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT    NOT NULL CHECK(role IN ('system','user','assistant','tool')),
    content     TEXT    NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    trace_id    TEXT    DEFAULT NULL,
    span_id     TEXT    DEFAULT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages (trace_id);

CREATE TABLE IF NOT EXISTS context_meta (
    session_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (session_id, key)
);
"""

class MessageSerialisationError(Exception):
    """Raised when a message dict cannot be serialised/deserialised to/from JSON."""
    pass

class _MsgEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if hasattr(o, 'model_dump'):
            return o.model_dump()
        if hasattr(o, '__dict__'):
            return vars(o)
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace')
        return str(o)

def serialize_message(msg: dict) -> str:
    try:
        return json.dumps(msg, cls=_MsgEncoder, separators=(',', ':'))
    except Exception as e:
        raise MessageSerialisationError(f"serialize failed: {e}") from e

def deserialize_message(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise MessageSerialisationError(f"deserialize failed: {e}") from e

@dataclass(slots=True)
class MessageRow:
    id: int
    session_id: str
    seq: int
    role: str
    content: str
    token_count: int
    trace_id: str | None
    span_id: str | None
    created_at: str

class MessageStore:
    """Async SQLite-backed message journal for untrimmed agent trajectories."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA_DDL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def append(
        self, session_id: str, seq: int, role: str, content_json: str, 
        token_count: int, trace_id: str | None = None, span_id: str | None = None
    ) -> None:
        assert self._conn is not None, "MessageStore is not open"
        query = """
            INSERT INTO messages (session_id, seq, role, content, token_count, trace_id, span_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        await self._conn.execute(query, (session_id, seq, role, content_json, token_count, trace_id, span_id))
        await self._conn.commit()

    async def append_batch(self, session_id: str, rows: list[MessageRow]) -> None:
        assert self._conn is not None, "MessageStore is not open"
        query = """
            INSERT INTO messages (session_id, seq, role, content, token_count, trace_id, span_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        data = [
            (session_id, r.seq, r.role, r.content, r.token_count, r.trace_id, r.span_id)
            for r in rows
        ]
        await self._conn.executemany(query, data)
        await self._conn.commit()

    async def latest(self, session_id: str, limit: int) -> list[MessageRow]:
        assert self._conn is not None, "MessageStore is not open"
        query = """
            SELECT id, session_id, seq, role, content, token_count, trace_id, span_id, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY seq DESC
            LIMIT ?
        """
        async with self._conn.execute(query, (session_id, limit)) as cursor:
            rows = await cursor.fetchall()
        
        # Reverse to chronological order
        return [MessageRow(*row) for row in reversed(rows)]

    async def total_tokens(self, session_id: str) -> int:
        assert self._conn is not None, "MessageStore is not open"
        query = "SELECT SUM(token_count) FROM messages WHERE session_id = ?"
        async with self._conn.execute(query, (session_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else 0

    async def max_seq(self, session_id: str) -> int | None:
        assert self._conn is not None, "MessageStore is not open"
        query = "SELECT MAX(seq) FROM messages WHERE session_id = ?"
        async with self._conn.execute(query, (session_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_meta(self, session_id: str, key: str, value: str) -> None:
        assert self._conn is not None, "MessageStore is not open"
        query = """
            INSERT INTO context_meta (session_id, key, value, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(session_id, key) DO UPDATE SET 
                value = excluded.value, 
                updated_at = excluded.updated_at
        """
        await self._conn.execute(query, (session_id, key, value))
        await self._conn.commit()

    async def get_meta(self, session_id: str, key: str) -> str | None:
        assert self._conn is not None, "MessageStore is not open"
        query = "SELECT value FROM context_meta WHERE session_id = ? AND key = ?"
        async with self._conn.execute(query, (session_id, key)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def snapshot_backup(self, backup_path: Path) -> None:
        """Creates a point-in-time snapshot of the entire active database."""
        assert self._conn is not None, "MessageStore is not open"
        if backup_path.exists():
            backup_path.unlink()  # VACUUM INTO requires the target to not exist
        await self._conn.execute(f"VACUUM INTO '{backup_path}'")

    async def archive_inactive_sessions(self, retention_days: int, archive_path: Path) -> int:
        """
        Moves sessions with no activity in the last `retention_days` to the archive DB,
        then deletes them from the active workspace DB to maintain query performance.
        """
        assert self._conn is not None, "MessageStore is not open"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 1. Attach the archive database to the current connection
        await self._conn.execute(f"ATTACH DATABASE '{archive_path}' AS archive")
        
        try:
            # 2. Ensure archive schema exists
            await self._conn.executescript(SCHEMA_DDL.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE IF NOT EXISTS archive."))
            
            # 3. Execute the cross-database move transaction
            await self._conn.execute("BEGIN IMMEDIATE")
            
            # Identify sessions where the most recent message is older than the cutoff
            find_stale_sessions = f"""
                CREATE TEMP TABLE stale_sessions AS 
                SELECT session_id FROM main.messages 
                GROUP BY session_id 
                HAVING MAX(created_at) < datetime('now', '-{retention_days} days')
            """
            await self._conn.execute(find_stale_sessions)
            
            # Copy messages and metadata to archive
            await self._conn.execute("""
                INSERT OR IGNORE INTO archive.messages (session_id, seq, role, content, token_count, trace_id, span_id, created_at)
                SELECT session_id, seq, role, content, token_count, trace_id, span_id, created_at 
                FROM main.messages 
                WHERE session_id IN stale_sessions
            """)
            
            await self._conn.execute("""
                INSERT OR IGNORE INTO archive.context_meta (session_id, key, value, updated_at)
                SELECT session_id, key, value, updated_at 
                FROM main.context_meta 
                WHERE session_id IN stale_sessions
            """)
            
            # Delete from active database (The strict append-only exception)
            cursor = await self._conn.execute("DELETE FROM main.messages WHERE session_id IN stale_sessions")
            archived_rows = cursor.rowcount
            await self._conn.execute("DELETE FROM main.context_meta WHERE session_id IN stale_sessions")
            
            await self._conn.execute("DROP TABLE stale_sessions")
            await self._conn.commit()
            
            return archived_rows
            
        except Exception:
            await self._conn.rollback()
            raise
        finally:
            await self._conn.execute("DETACH DATABASE archive")