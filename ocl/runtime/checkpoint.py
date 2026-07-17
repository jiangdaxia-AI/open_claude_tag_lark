"""Checkpoint manager — persist agent execution state for crash recovery.

Saves the following state to a dedicated SQLite DB (checkpoints.db):
  - messages list (the conversation so far)
  - current round number
  - sandbox session ID (for reattaching to sandboxes)

On process restart, the agent loop can call resume() to check for an
existing checkpoint and continue from where it left off.

Storage: separate SQLite DB (checkpoints.db) to avoid coupling with
task_store or message_store schemas.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiosqlite

from ocl.config import settings

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)

_CREATE_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    messages    TEXT NOT NULL,           -- JSON array of message dicts
    round_num   INTEGER NOT NULL DEFAULT 0,
    sandbox_id  TEXT DEFAULT '',          -- sandbox session ID (if any)
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec'))
);
"""

_CREATE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoint_session
    ON checkpoints(channel_id, agent_id, session_id);
"""


async def _get_checkpoint_db() -> aiosqlite.Connection:
    """Get or create a DB connection for checkpoint storage."""
    db_path = settings.data_dir / "workspaces" / "checkpoints.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_CHECKPOINTS + _CREATE_INDEX)
    await db.commit()
    return db


class CheckpointManager:
    """Manages agent execution checkpoints for crash recovery."""

    async def save(
        self,
        rt: "AgentRuntime",
        messages: list[dict],
        round_num: int,
        sandbox_id: str = "",
    ) -> None:
        """Save current execution state to SQLite.

        Called after each round in the agent loop. Failures are logged
        but do not interrupt execution.
        """
        if not rt.session_id:
            return  # No session ID — nothing to checkpoint

        messages_json = json.dumps(messages, ensure_ascii=False)

        db = await _get_checkpoint_db()
        try:
            await db.execute(
                """INSERT INTO checkpoints (channel_id, agent_id, session_id, messages, round_num, sandbox_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, unixepoch('now','subsec'))
                   ON CONFLICT(channel_id, agent_id, session_id)
                   DO UPDATE SET messages = excluded.messages,
                                  round_num = excluded.round_num,
                                  sandbox_id = excluded.sandbox_id,
                                  updated_at = unixepoch('now','subsec')""",
                (rt.channel_id, rt.agent_id, rt.session_id, messages_json, round_num, sandbox_id),
            )
            await db.commit()
        except Exception:
            logger.warning("Failed to save checkpoint for session %s", rt.session_id, exc_info=True)
        finally:
            await db.close()

    async def load(
        self,
        channel_id: str,
        agent_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Load a saved checkpoint. Returns None if not found."""
        if not session_id:
            return None

        db = await _get_checkpoint_db()
        try:
            async with db.execute(
                """SELECT * FROM checkpoints
                   WHERE channel_id = ? AND agent_id = ? AND session_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (channel_id, agent_id, session_id),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return None

            return {
                "messages": json.loads(row["messages"]),
                "round_num": row["round_num"],
                "sandbox_id": row["sandbox_id"],
                "updated_at": row["updated_at"],
            }
        except Exception:
            logger.warning("Failed to load checkpoint for session %s", session_id, exc_info=True)
            return None
        finally:
            await db.close()

    async def resume(self, rt: "AgentRuntime") -> dict[str, Any] | None:
        """Check for an existing checkpoint and return state if found.

        Returns None if no checkpoint exists or it's stale (older than 1 hour).
        """
        if not rt.session_id:
            return None

        state = await self.load(rt.channel_id, rt.agent_id, rt.session_id)
        if not state:
            return None

        # Check staleness: ignore checkpoints older than 1 hour
        age = time.time() - state.get("updated_at", 0)
        if age > 3600:
            logger.info("Checkpoint for session %s is stale (%.0fs old), ignoring", rt.session_id, age)
            return None

        logger.info(
            "Found checkpoint for session %s: round %d, %d messages",
            rt.session_id, state["round_num"], len(state["messages"]),
        )
        return state

    async def clear(self, channel_id: str, agent_id: str, session_id: str) -> None:
        """Delete a checkpoint after successful completion."""
        if not session_id:
            return

        db = await _get_checkpoint_db()
        try:
            await db.execute(
                """DELETE FROM checkpoints
                   WHERE channel_id = ? AND agent_id = ? AND session_id = ?""",
                (channel_id, agent_id, session_id),
            )
            await db.commit()
        except Exception:
            logger.warning("Failed to clear checkpoint for session %s", session_id, exc_info=True)
        finally:
            await db.close()


# ── Global singleton ─────────────────────────────────────────────────────────

_global_checkpoint_manager: CheckpointManager | None = None


def get_checkpoint_manager() -> CheckpointManager:
    """Get or create the global CheckpointManager instance."""
    global _global_checkpoint_manager
    if _global_checkpoint_manager is None:
        _global_checkpoint_manager = CheckpointManager()
    return _global_checkpoint_manager
