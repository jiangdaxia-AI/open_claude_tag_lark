"""Thread unfollow — agents can stop receiving messages from a thread.

When an agent calls the unfollow tool, subsequent messages in that thread
won't trigger that agent. The agent can re-follow by being @mentioned again.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_UNFOLLOW = """
CREATE TABLE IF NOT EXISTS unfollowed_threads (
    channel_id  TEXT NOT NULL,
    thread_ts   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    unfollowed_at REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    PRIMARY KEY (channel_id, thread_ts, agent_id)
);
"""


async def _get_db() -> aiosqlite.Connection:
    db_path = settings.data_dir / "workspaces" / "threads.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    await db.executescript(_CREATE_UNFOLLOW)
    await db.commit()
    return db


async def unfollow_thread(channel_id: str, thread_ts: str, agent_id: str) -> None:
    db = await _get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO unfollowed_threads (channel_id, thread_ts, agent_id) VALUES (?, ?, ?)",
            (channel_id, thread_ts, agent_id),
        )
        await db.commit()
    finally:
        await db.close()


async def is_unfollowed(channel_id: str, thread_ts: str, agent_id: str) -> bool:
    db = await _get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM unfollowed_threads WHERE channel_id = ? AND thread_ts = ? AND agent_id = ?",
            (channel_id, thread_ts, agent_id),
        ) as cursor:
            return await cursor.fetchone() is not None
    finally:
        await db.close()


async def refollow_thread(channel_id: str, thread_ts: str, agent_id: str) -> None:
    db = await _get_db()
    try:
        await db.execute(
            "DELETE FROM unfollowed_threads WHERE channel_id = ? AND thread_ts = ? AND agent_id = ?",
            (channel_id, thread_ts, agent_id),
        )
        await db.commit()
    finally:
        await db.close()
