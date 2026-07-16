"""SQLite + FTS5 message store — one DB per workspace, one table per channel."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import aiosqlite

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seq         INTEGER NOT NULL,           -- monotonic per-channel sequence number
    ts          TEXT NOT NULL,
    thread_ts   TEXT,
    channel_id  TEXT NOT NULL,
    role        TEXT NOT NULL,              -- 'user' | 'assistant'
    user_id     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_calls  INTEGER DEFAULT 0,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);
"""

_CREATE_SEQ_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages(channel_id, seq);
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    display_name,
    content='messages',
    content_rowid='id'
);
"""

_CREATE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, display_name)
    VALUES (new.id, new.content, new.display_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, display_name)
    VALUES ('delete', old.id, old.content, old.display_name);
END;
"""

_CREATE_PROCESSED_EVENTS = """
CREATE TABLE IF NOT EXISTS processed_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    received_at REAL NOT NULL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_processed_events_received_at
    ON processed_events(received_at);
"""

_CREATE_BOOKMARKS = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_channel_user ON bookmarks(channel_id, user_id);
"""


class MessageStore:
    def __init__(self, db_path: Path, channel_id: str) -> None:
        self._db_path = db_path
        self._channel_id = channel_id
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_CREATE_MESSAGES + _CREATE_FTS + _CREATE_TRIGGERS + _CREATE_PROCESSED_EVENTS + _CREATE_SEQ_INDEX + _CREATE_BOOKMARKS)
        await self._db.commit()

    async def add_message(
        self,
        ts: str,
        role: str,
        user_id: str,
        display_name: str,
        content: str,
        thread_ts: str | None = None,
        tool_calls: int = 0,
    ) -> int:
        """Insert a message. Returns the new seq number."""
        assert self._db
        # Allocate a per-channel monotonic seq
        async with self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE channel_id = ?",
            (self._channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
            seq = row[0]
        await self._db.execute(
            """INSERT INTO messages (seq, ts, thread_ts, channel_id, role, user_id, display_name, content, tool_calls)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seq, ts, thread_ts, self._channel_id, role, user_id, display_name, content, tool_calls),
        )
        await self._db.commit()
        return seq

    async def get_recent_messages(self, limit: int = 50) -> list[aiosqlite.Row]:
        assert self._db
        async with self._db.execute(
            """SELECT ts, role, user_id, display_name, content
               FROM messages
               WHERE channel_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (self._channel_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return list(reversed(rows))  # return chronologically

    async def search(self, query: str, limit: int = 10) -> list[aiosqlite.Row]:
        """Full-text search across channel messages."""
        assert self._db
        async with self._db.execute(
            """SELECT m.ts, m.role, m.display_name, m.content
               FROM messages_fts f
               JOIN messages m ON m.id = f.rowid
               WHERE messages_fts MATCH ? AND m.channel_id = ?
               ORDER BY rank
               LIMIT ?""",
            (query, self._channel_id, limit),
        ) as cursor:
            return await cursor.fetchall()

    async def is_event_processed(self, event_id: str) -> bool:
        """Return True if event_id has been seen before."""
        assert self._db
        async with self._db.execute(
            "SELECT 1 FROM processed_events WHERE event_id = ?",
            (event_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def mark_event_processed(self, event_id: str, event_type: str) -> None:
        """Record an event_id. Idempotent — re-inserting is a no-op."""
        assert self._db
        await self._db.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, event_type) VALUES (?, ?)",
            (event_id, event_type),
        )
        await self._db.commit()

    async def cleanup_old_events(self, days: int = 7) -> int:
        """Delete events older than `days`. Returns number of rows deleted."""
        assert self._db
        cutoff = __import__("time").time() - days * 86400
        cursor = await self._db.execute(
            "DELETE FROM processed_events WHERE received_at < ?",
            (cutoff,),
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── seq-based queries (for freshness-hold) ──

    async def get_last_seq(self) -> int:
        """Return the highest seq in this channel, or 0 if empty."""
        assert self._db
        async with self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE channel_id = ?",
            (self._channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] or 0

    async def get_messages_since(self, since_seq: int, limit: int = 10) -> list:
        """Return messages with seq > since_seq (for freshness-hold checks)."""
        assert self._db
        async with self._db.execute(
            """SELECT seq, ts, role, user_id, display_name, content
               FROM messages WHERE channel_id = ? AND seq > ?
               ORDER BY seq ASC LIMIT ?""",
            (self._channel_id, since_seq, limit),
        ) as cursor:
            return await cursor.fetchall()

    async def get_message_by_seq(self, seq: int) -> dict | None:
        """Return a single message by seq number."""
        assert self._db
        async with self._db.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND seq = ?",
            (self._channel_id, seq),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ── bookmarks ──

    async def add_bookmark(self, user_id: str, message_id: str) -> int:
        """Bookmark a message. Returns bookmark id."""
        assert self._db
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO bookmarks (channel_id, user_id, message_id) VALUES (?, ?, ?)",
            (self._channel_id, user_id, message_id),
        )
        await self._db.commit()
        return cursor.lastrowid or 0

    async def remove_bookmark(self, user_id: str, message_id: str) -> None:
        assert self._db
        await self._db.execute(
            "DELETE FROM bookmarks WHERE channel_id = ? AND user_id = ? AND message_id = ?",
            (self._channel_id, user_id, message_id),
        )
        await self._db.commit()

    async def list_bookmarks(self, user_id: str) -> list:
        assert self._db
        async with self._db.execute(
            """SELECT b.message_id, b.created_at, m.content, m.display_name
               FROM bookmarks b
               LEFT JOIN messages m ON m.ts = b.message_id AND m.channel_id = b.channel_id
               WHERE b.channel_id = ? AND b.user_id = ?
               ORDER BY b.created_at DESC""",
            (self._channel_id, user_id),
        ) as cursor:
            return await cursor.fetchall()

    # ── thread management ──

    async def get_thread_messages(self, thread_ts: str, limit: int = 50) -> list:
        """Return all messages in a thread."""
        assert self._db
        async with self._db.execute(
            """SELECT seq, ts, role, user_id, display_name, content
               FROM messages WHERE channel_id = ? AND thread_ts = ?
               ORDER BY seq ASC LIMIT ?""",
            (self._channel_id, thread_ts, limit),
        ) as cursor:
            return await cursor.fetchall()


_stores: dict[tuple[str, str], MessageStore] = {}


async def get_store(workspace_id: str, channel_id: str) -> MessageStore:
    key = (workspace_id, channel_id)
    if key not in _stores:
        db_path = settings.data_dir / "workspaces" / workspace_id / "messages.db"
        store = MessageStore(db_path=db_path, channel_id=channel_id)
        await store.open()
        _stores[key] = store
    return _stores[key]
