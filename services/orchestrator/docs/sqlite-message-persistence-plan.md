# Implementation Plan: SQLite Message History Persistence

> Status: **PLAN ONLY - no code has been written.**
> Target module: `services/orchestrator/src/memory/context_manager.py`
> Revision: 3 (append-only, context_meta, serialization hardening, busy_timeout, trace/span IDs, scrubbing contract, VACUUM INTO archive)

---

## 1. Problem Statement

`SlidingContextManager` keeps its entire message window in-process (`state.history`). When the
orchestrator process restarts (crash, deploy, daemon restart) every prior message is lost.
We need a durable store so that on next start-up the manager can **rehydrate** its sliding
window from disk, and so that **every appended message is durably journaled**.

Downstream observability tooling (OpenTelemetry / Arize Phoenix) requires the **untrimmed,
full conversation trajectory** to remain queryable. This mandates an **append-only** journal
in the database; truncation is an in-memory concern only.

Trace correlation: every message row must carry the `trace_id` and `span_id` from the OTel
context already active in the agent loop, so Phoenix can join messages to spans without a
second lookup table.

---

## 2. Design Decisions

| Concern | Decision | Rationale |
|---|---|---|
| Storage engine | **SQLite** (single file per workspace) | Zero-daemon, ACID, sequential reads O(n) |
| Async driver | **`aiosqlite`** | Entire orchestrator is async; avoids blocking the event loop |
| Session key | `AgentState.run_id` | Already present; no new ID generation |
| Migration strategy | **CREATE TABLE IF NOT EXISTS** | Two-table schema, no Alembic needed. Trace/span columns included in P0 DDL to avoid a later migration. |
| Write mode | **Append-only (no UPDATE / DELETE on messages)** | Phoenix/OTel need the full trajectory; compaction truncates in-memory only |
| Summary & derived state | **Separate `context_meta` table** | Keeps messages pure; avoids sentinel rows |
| Trace correlation | **`trace_id` + `span_id` columns on `messages`** | OTel spans already exist in the agent loop; adding them in P0 avoids a schema migration. Nullable (system/bootstrap messages may not have a span). |
| Secrets scrubbing | **Upstream intercept in manager, before `store.append()`** | The persistence layer is a dumb write; sanitisation is the manager's responsibility. Keeps the store generic. |
| WAL mode | `PRAGMA journal_mode=WAL` | Concurrent reader + writer without lock contention |
| Lock contention | `PRAGMA busy_timeout=5000` | Prevents SQLITE_BUSY when VS Code ext and daemon race |
| Token count | INTEGER per row | Avoids re-tokenize on hydration |
| Serialization | Custom encoder + `MessageSerialisationError` + 7 round-trip tests | Prevents infinite-retry bugs |
| Archival (P3) | **`VACUUM INTO` to `orchestrator_archive.db`** | Keeps active DB lean; Phoenix queries target the hot file only |

---

## 3. Schema

```sql
-- <workspace>/.orchestrator/orchestrator.db

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,          -- AgentState.run_id
    seq         INTEGER NOT NULL,          -- monotonic 0-based; never negative
    role        TEXT    NOT NULL CHECK(role IN ('system','user','assistant','tool')),
    content     TEXT    NOT NULL,          -- JSON-serialised full message dict
    token_count INTEGER NOT NULL DEFAULT 0,
    -- OTel correlation (nullable: bootstrap/system messages may lack a span)
    trace_id    TEXT    DEFAULT NULL,      -- W3C Trace Context trace-id (32 hex chars)
    span_id     TEXT    DEFAULT NULL,      -- W3C Trace Context span-id (16 hex chars)
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages (trace_id);

CREATE TABLE IF NOT EXISTS context_meta (
    session_id  TEXT NOT NULL,
    key         TEXT NOT NULL,          -- 'summary', 'user_goal', 'last_compaction_seq'
    value       TEXT NOT NULL,          -- JSON-serialised value
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (session_id, key)
);
```

### 3.1 trace_id / span_id population

- The agent loop already maintains an active OTel `Span` via the existing instrumentation.
- At each `store.append(...)` call site inside `SlidingContextManager`, the manager reads:
  ```python
  from opentelemetry.trace import get_current_span
  span = get_current_span()
  ctx  = span.get_span_context()
  trace_id = format(ctx.trace_id, '032x') if ctx.trace_id else None
  span_id  = format(ctx.span_id,  '016x') if ctx.span_id else None
  ```
- Bootstrap / system messages (e.g. the initial goal injection) will have `NULL` trace/span.
- Phoenix can filter `WHERE trace_id IS NOT NULL` for span-joined queries.

### 3.2 context_meta usage

| key | value | When written |
|---|---|---|
| `summary` | compressed text (JSON string) | After `_chunked_compaction` |
| `user_goal` | goal text | When set/updated |
| `last_compaction_seq` | int as JSON | After compaction |

Upsert: `INSERT ... ON CONFLICT(session_id,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at`

---

## 4. New Module: `services/orchestrator/src/memory/persistence.py`

### 4.1 Public API

```python
class MessageStore:
    async def open(self) -> None: ...
    async def close(self) -> None: ...

    # append-only writes
    async def append(self, session_id, seq, role, content_json, token_count,
                     trace_id: str | None = None, span_id: str | None = None) -> None: ...
    async def append_batch(self, session_id, rows: list[MessageRow]) -> None: ...

    # reads
    async def latest(self, session_id, limit) -> list[MessageRow]: ...
    async def total_tokens(self, session_id) -> int: ...
    async def max_seq(self, session_id) -> int | None: ...

    # context_meta
    async def set_meta(self, session_id, key, value) -> None: ...
    async def get_meta(self, session_id, key) -> str | None: ...

    # lifecycle / archival
    async def archive_session(self, session_id) -> None: ...
    async def vacuum_into(self, archive_path: Path) -> None:
        """Move all archived (non-active) rows to a separate DB file via VACUUM INTO."""
```

### 4.2 Connection setup

```python
async def open(self) -> None:
    self._conn = await aiosqlite.connect(self._db_path)
    await self._conn.execute("PRAGMA journal_mode=WAL")
    await self._conn.execute("PRAGMA busy_timeout=5000")
    await self._conn.executescript(SCHEMA_DDL)  # CREATE TABLE IF NOT EXISTS ...
    await self._conn.commit()
```

### 4.3 Serialization helpers

```python
class MessageSerialisationError(Exception):
    """Raised when a message dict cannot be serialised/deserialised to/from JSON."""

class _MsgEncoder(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, 'model_dump'): return o.model_dump()
        if hasattr(o, '__dict__'): return vars(o)
        if isinstance(o, bytes): return o.decode('utf-8', errors='replace')
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
```

The agent-loop retry guard catches `MessageSerialisationError` specifically and applies a
bounded retry / circuit-breaker instead of looping infinitely.

---

## 5. Integration in SlidingContextManager

### 5.1 Constructor (backwards-compatible)

```python
SlidingContextManager(max_tokens=32768, summary_budget=4096,
                      message_store: MessageStore | None = None)
```

### 5.2 `__init__` - Rehydration

1. `rows = await store.latest(run_id, limit=MAX_HYDRATION_ROWS)`
2. `state.history = [deserialize_message(r.content) for r in rows]`
3. Restore `state.summary`, `state.user_goal` from `context_meta`
4. `state.total_tokens = await store.total_tokens(run_id)`

### 5.3 Append path (write)

```python
# PSEUDOCODE inside the manager's append helper:
async def _persist_message(self, msg: dict, token_count: int) -> None:
    if self._store is None:
        return  # ephemeral mode

    # --- SECRETS SCRUBBING (upstream of store.append) ---
    scrubbed = self._secrets_scrubber.sanitize(msg)  # see 5.4

    # --- TRACE CORRELATION ---
    from opentelemetry.trace import get_current_span
    span  = get_current_span()
    ctx   = span.get_span_context()
    t_id  = format(ctx.trace_id, '032x') if ctx.trace_id else None
    s_id  = format(ctx.span_id,  '016x') if ctx.span_id  else None

    # --- PERSIST ---
    seq = (await self._store.max_seq(self._run_id) or -1) + 1
    await self._store.append(
        session_id   = self._run_id,
        seq          = seq,
        role         = scrubbed.get('role', 'user'),
        content_json = serialize_message(scrubbed),
        token_count  = token_count,
        trace_id     = t_id,
        span_id      = s_id,
    )
```

### 5.4 Secrets Scrubbing Contract

- **Location:** `SlidingContextManager` (or a thin `SecretsScrubber` collaborator injected
  into the manager). NOT in `persistence.py`.
- **Trigger:** Every call to `_persist_message` invokes the scrubber **before**
  `store.append(...)`. The scrubber walks the message dict recursively and replaces
  known secret patterns (API keys, bearer tokens, connection strings) with a
  `[REDACTED]` placeholder.
- **Configuration:** The scrubber's pattern list is sourced from existing project
  settings (e.g. `.orchestrator/secrets_patterns.json`) and can be extended per-workspace.
- **Audit log:** Scrubbing events are structured-logged at WARNING level with the key's
  redacted prefix (first 4 chars) for correlation in Phoenix.
- **Guarantee:** The `content` column NEVER contains raw secrets. This is a hard
  invariant enforced by the scrubber, not by downstream filtering.

### 5.5 `_chunked_compaction` - Post-Compaction

```python
# IN-MEMORY: truncate window
state.history = [summary_msg] + tail_msgs

# PERSIST: update derived metadata
await self._store.set_meta(run_id, "summary", json.dumps(summary_text))
await self._store.set_meta(run_id, "last_compaction_seq", str(compacted_up_to_seq))

# messages table is UNTOUCHED - full trajectory preserved for Phoenix.
```

### 5.6 Sequence numbering
- 0-based, always >= 0, monotonic per session.
- Next = `MAX(seq) + 1`.
- Gap-free across restarts.

---

## 6. Edge Cases & Risks

### 6.1 Crash mid-append
WAL + single commit per append: at most last message lost. Rehydration + budget re-check protect against overflow.

### 6.2 Summary rehydration
Read from `context_meta`. Fallback: re-run compaction if `total_tokens > max_tokens`.

### 6.3 Concurrency
WAL + `busy_timeout=5000` on every connection. VS Code ext opens `?mode=ro`. No SQLITE_BUSY expected.

### 6.4 JSON serialization failures
`MessageSerialisationError` domain type; agent-loop retry guard treats it as non-retryable (circuit-break). Encoder `default` covers pydantic/bytes/unknown.

### 6.5 DB file location
Default: `<workspace_root>/.orchestrator/orchestrator.db`. Override: `ORCH_DB_PATH` env. Directory in `.gitignore`.

### 6.6 trace_id / span_id NULLs
Bootstrap messages (initial goal, system prompt) will have NULL trace/span. Phoenix queries must use `WHERE trace_id IS NOT NULL` for span-joined views. This is by design, not a bug.

---

## 7. Testing Plan

### 7.1 Core (tests 1-7)
| # | Test |
|---|---|
| 1 | append_and_readback |
| 2 | rehydrate_populates_history |
| 3 | crash_recovery_no_partial_row |
| 4 | concurrent_read_write_no_deadlock |
| 5 | seq_continuity_across_restart |
| 6 | summary_meta_roundtrip_messages_untouched |
| 7 | append_only_invariant_count_stable_after_compaction |

### 7.2 Serialization Gate (tests 8-14, HARD CI GATE)
| # | Test |
|---|---|
| 8 | roundtrip_text_message |
| 9 | roundtrip_tool_use_block |
| 10 | roundtrip_tool_result_block |
| 11 | roundtrip_image_block_base64 |
| 12 | roundtrip_pydantic_model_in_input |
| 13 | roundtrip_deeply_nested_lists |
| 14 | corrupted_json_raises_MessageSerialisationError_not_JSONDecodeError |

### 7.3 Multi-process (tests 15-16)
| # | Test |
|---|---|
| 15 | busy_timeout_allows_reader_to_wait_writer_out |
| 16 | ro_connection_succeeds_during_write_txn |

### 7.4 Trace correlation (tests 17-19)
| # | Test |
|---|---|
| 17 | trace_id_span_id_populated_from_active_otel_span |
| 18 | trace_id_null_for_bootstrap_message |
| 19 | phoenix_query_join: SELECT * FROM messages WHERE trace_id = ? returns expected rows |

### 7.5 Scrubbing (tests 20-21)
| # | Test |
|---|---|
| 20 | secret_pattern_replaced_before_persist |
| 21 | scrubbed_content_still_valid_json_after_redaction |

---

## 8. Dependencies

```toml
# pyproject.toml (services/orchestrator)
aiosqlite >= 0.20
# opentelemetry-api and opentelemetry-sdk already present (existing instrumentation)
```

---

## 9. Phased Rollout

| Phase | Scope | Risk |
|---|---|---|
| **P0** | `persistence.py` (incl. serializer, trace_id/span_id columns), schema DDL, tests 1-14, 17-19. SecretsScrubber stub. No manager changes. | Low |
| **P1** | Wire `message_store` into manager. ORCH_PERSIST=1 gate. Tests 20-21. | Medium |
| **P2** | `context_meta` writes. VS Code read-only viewer. Multi-process tests 15-16. | Low |
| **P3** | Archival: `VACUUM INTO orchestrator_archive.db` for sessions older than retention window. Active DB stays lean. Backup script. Phoenix integration validation. | Low |

### P3 Archival Detail

```python
async def vacuum_into(self, archive_path: Path) -> None:
    """
    1. VACUUM INTO '<archive_path>'  -- creates a copy of the current DB
    2. DELETE FROM messages WHERE session_id IN (SELECT ... WHERE created_at < retention_cutoff)
       (This DELETE is the ONE exception to the append-only rule; it only fires
        for sessions that have been fully archived and are no longer active.)
    3. Same for context_meta rows belonging to archived sessions.
    4. WAL checkpoint.
    """
```

- The archive file (`orchestrator_archive.db`) is the permanent record; the active DB is a working set.
- Phoenix queries should target the active DB by default; historical queries can open the archive.
- `VACUUM INTO` is atomic (produces a consistent snapshot) and requires no external tooling.

---

## 10. File Inventory

| File | Action |
|---|---|
| `services/orchestrator/src/memory/persistence.py` | CREATE |
| `services/orchestrator/src/memory/scrubber.py` | CREATE (SecretsScrubber) |
| `services/orchestrator/src/memory/context_manager.py` | MODIFY |
| `services/orchestrator/src/state.py` | No change |
| `services/orchestrator/tests/test_persistence.py` | CREATE (tests 1-7) |
| `services/orchestrator/tests/test_serialization.py` | CREATE (tests 8-14, CI gate) |
| `services/orchestrator/tests/test_trace_correlation.py` | CREATE (tests 17-19) |
| `services/orchestrator/tests/test_scrubbing.py` | CREATE (tests 20-21) |
| `services/orchestrator/tests/test_multi_process.py` | CREATE (tests 15-16) |
| `services/orchestrator/tests/test_context_manager_persist.py` | CREATE (integration) |
| `services/orchestrator/pyproject.toml` | MODIFY (add aiosqlite) |
| `.gitignore` | MODIFY (add `.orchestrator/`, `orchestrator_archive.db`) |

---

## 11. Resolved Decisions (formerly Open Questions)

| # | Question | Resolution |
|---|---|---|
| 1 | Trace/span columns: P0 or later? | **P0.** Adding `trace_id TEXT DEFAULT NULL` and `span_id TEXT DEFAULT NULL` to the initial DDL avoids a schema migration. Nullable for bootstrap messages. |
| 2 | Where does secrets scrubbing happen? | **Upstream in the manager**, before `store.append()`. The `SecretsScrubber` collaborator sanitises the message dict; the persistence layer is a dumb write. Hard invariant: `content` column never contains raw secrets. |
| 3 | Archive strategy: same file or separate? | **Separate file** (`orchestrator_archive.db`) via `VACUUM INTO`. Keeps the active workspace DB lean for Phoenix queries. The one permitted DELETE fires only for fully-archived, inactive sessions. |
