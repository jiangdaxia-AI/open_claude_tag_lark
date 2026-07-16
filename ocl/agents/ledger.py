"""Agent execution ledger — records every agent run for audit and debugging.

Each ledger entry captures:
  - source event (who triggered, what message)
  - agent identity (agent_id, channel)
  - context snapshot (system prompt summary, message count)
  - tool call chain (every tool invoked, args, result)
  - delegation chain (which agents were woken)
  - final output (text length, message_id)
  - timing (start, end, duration)
  - token usage (if available)

Stored in SQLite (ledger.db) for queryability. Also exposed via web admin.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    trigger_user_id TEXT NOT NULL,
    trigger_message TEXT NOT NULL,
    delegation_depth INTEGER DEFAULT 0,
    upstream_chain  TEXT DEFAULT '',         -- JSON list of upstream agent_ids
    tool_calls      TEXT DEFAULT '[]',       -- JSON list of {name, args, result, duration}
    final_text      TEXT DEFAULT '',
    final_message_id TEXT DEFAULT '',
    streamed        INTEGER DEFAULT 0,       -- 1 if response was streamed
    started_at      REAL NOT NULL,
    ended_at        REAL,
    duration_ms     INTEGER,
    status          TEXT DEFAULT 'running',  -- running | completed | failed | cancelled
    error           TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ledger_channel ON ledger(channel_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_agent ON ledger(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger(status);
"""


@dataclass
class LedgerEntry:
    """In-memory builder for a ledger record. Call finalize() to persist."""
    channel_id: str
    agent_id: str
    trigger_user_id: str
    trigger_message: str
    delegation_depth: int = 0
    upstream_chain: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    _id: int | None = None
    _tool_calls: list[dict] = field(default_factory=list)
    _final_text: str = ""
    _final_message_id: str = ""
    _streamed: bool = False
    _status: str = "running"
    _error: str = ""

    def record_tool_call(self, name: str, args: dict, result: str, duration_ms: int) -> None:
        self._tool_calls.append({
            "name": name,
            "args": _truncate(json.dumps(args, ensure_ascii=False), 500),
            "result": _truncate(str(result), 500),
            "duration_ms": duration_ms,
        })

    def set_output(self, text: str, message_id: str, streamed: bool = False) -> None:
        self._final_text = _truncate(text, 2000)
        self._final_message_id = message_id
        self._streamed = streamed

    def set_error(self, error: str) -> None:
        self._error = _truncate(error, 1000)
        self._status = "failed"

    def set_cancelled(self) -> None:
        self._status = "cancelled"


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"


async def _get_ledger_db() -> aiosqlite.Connection:
    db_path = settings.data_dir / "workspaces" / "ledger.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_LEDGER)
    await db.commit()
    return db


async def create_entry(entry: LedgerEntry) -> int:
    """Insert a ledger entry and return its id."""
    db = await _get_ledger_db()
    try:
        cursor = await db.execute(
            """INSERT INTO ledger (channel_id, agent_id, trigger_user_id, trigger_message,
               delegation_depth, upstream_chain, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry.channel_id, entry.agent_id, entry.trigger_user_id, _truncate(entry.trigger_message, 500),
             entry.delegation_depth, json.dumps(entry.upstream_chain), entry.started_at, entry._status),
        )
        await db.commit()
        entry._id = cursor.lastrowid
        return entry._id or 0
    finally:
        await db.close()


async def finalize_entry(entry: LedgerEntry) -> None:
    """Update a ledger entry with final results."""
    if entry._id is None:
        return
    ended_at = time.time()
    duration_ms = int((ended_at - entry.started_at) * 1000)
    if entry._status == "running":
        entry._status = "completed"
    db = await _get_ledger_db()
    try:
        await db.execute(
            """UPDATE ledger SET tool_calls = ?, final_text = ?, final_message_id = ?,
               streamed = ?, ended_at = ?, duration_ms = ?, status = ?, error = ?
               WHERE id = ?""",
            (json.dumps(entry._tool_calls, ensure_ascii=False),
             entry._final_text, entry._final_message_id, 1 if entry._streamed else 0,
             ended_at, duration_ms, entry._status, entry._error, entry._id),
        )
        await db.commit()
    finally:
        await db.close()


async def list_entries(channel_id: str | None = None, agent_id: str | None = None,
                       status: str | None = None, limit: int = 50) -> list[dict]:
    """Query ledger entries."""
    db = await _get_ledger_db()
    try:
        query = "SELECT * FROM ledger WHERE 1=1"
        params: list[Any] = []
        if channel_id:
            query += " AND channel_id = ?"
            params.append(channel_id)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_entry(entry_id: int) -> dict | None:
    db = await _get_ledger_db()
    try:
        async with db.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()
