"""Reminder system: schedule, list, cancel reminders via APScheduler.

Reminders are persisted in SQLite and fired by APScheduler.
When a reminder fires, the agent posts a message in the channel.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_REMINDERS = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL DEFAULT 'default',
    message     TEXT NOT NULL,
    target      TEXT DEFAULT '',
    remind_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    fired_at    REAL
);
"""


async def _get_db() -> aiosqlite.Connection:
    db_path = settings.data_dir / "workspaces" / "reminders.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_REMINDERS)
    await db.commit()
    return db


async def reminder_schedule(
    channel_id: str,
    agent_id: str,
    message: str,
    remind_at: str,
    target: str = "",
) -> dict[str, Any]:
    """Schedule a reminder. remind_at should be ISO 8601 datetime string."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO reminders (channel_id, agent_id, message, target, remind_at)
               VALUES (?, ?, ?, ?, ?)""",
            (channel_id, agent_id, message, target, remind_at),
        )
        await db.commit()
        reminder_id = cursor.lastrowid
        logger.info("Reminder #%d scheduled for %s in channel %s", reminder_id, remind_at, channel_id)

        _scheduler: AsyncIOScheduler | None = _scheduler_ref
        if _scheduler is not None:
            try:
                dt = datetime.fromisoformat(remind_at)
                _scheduler.add_job(
                    _fire_reminder,
                    trigger=DateTrigger(run_date=dt),
                    args=[reminder_id, channel_id, agent_id, message, target],
                    id=f"reminder-{reminder_id}",
                    replace_existing=True,
                )
            except Exception as exc:
                logger.warning("Failed to schedule reminder with APScheduler: %s", exc)

        return {
            "id": reminder_id,
            "channel_id": channel_id,
            "agent_id": agent_id,
            "message": message,
            "target": target,
            "remind_at": remind_at,
            "status": "pending",
        }
    finally:
        await db.close()


async def reminder_list(channel_id: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    db = await _get_db()
    try:
        query = "SELECT * FROM reminders WHERE channel_id = ? AND status = 'pending'"
        params: list[Any] = [channel_id]
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY remind_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def reminder_cancel(reminder_id: int) -> bool:
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        await db.commit()
        affected = db.total_changes > 0
        _scheduler: AsyncIOScheduler | None = _scheduler_ref
        if _scheduler is not None and affected:
            try:
                _scheduler.remove_job(f"reminder-{reminder_id}")
            except Exception:
                pass
        return affected
    finally:
        await db.close()


# ── APScheduler integration ──

_scheduler_ref: AsyncIOScheduler | None = None
_gateway_ref = None


def init_reminder_scheduler(scheduler: AsyncIOScheduler, gateway) -> None:
    """Wire up the APScheduler and Gateway for firing reminders."""
    global _scheduler_ref, _gateway_ref
    _scheduler_ref = scheduler
    _gateway_ref = gateway


async def _fire_reminder(
    reminder_id: int,
    channel_id: str,
    agent_id: str,
    message: str,
    target: str,
) -> None:
    """Fire a reminder: mark as fired and post to the channel."""
    logger.info("Firing reminder #%d in channel %s", reminder_id, channel_id)

    db = await _get_db()
    try:
        await db.execute(
            "UPDATE reminders SET status = 'fired', fired_at = unixepoch('now','subsec') WHERE id = ?",
            (reminder_id,),
        )
        await db.commit()
    finally:
        await db.close()

    if _gateway_ref is not None:
        target_str = f"@{target} " if target else ""
        text = f"⏰ Reminder: {target_str}{message}"
        try:
            await _gateway_ref.send_message(chat_id=channel_id, text=text)
        except Exception as exc:
            logger.error("Failed to send reminder message: %s", exc)


# ── LiteLLM tool schemas ──

REMINDER_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "reminder_schedule",
            "description": "Schedule a reminder for a future time",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Reminder message text"},
                    "remind_at": {
                        "type": "string",
                        "description": "When to fire (ISO 8601: 2026-07-15T16:00:00 or 2026-07-15 16:00)",
                    },
                    "target": {"type": "string", "description": "Who to remind (@username, optional)"},
                },
                "required": ["message", "remind_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_list",
            "description": "List pending reminders in this channel",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_cancel",
            "description": "Cancel a pending reminder",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "description": "The reminder ID to cancel"},
                },
                "required": ["reminder_id"],
            },
        },
    },
]


async def dispatch_reminder_tool(
    fn_name: str, args: dict[str, Any], channel_id: str, agent_id: str
) -> Any:
    if fn_name == "reminder_schedule":
        result = await reminder_schedule(
            channel_id=channel_id,
            agent_id=agent_id,
            message=args["message"],
            remind_at=args["remind_at"],
            target=args.get("target", ""),
        )
        return f"Reminder #{result['id']} scheduled for {result['remind_at']}: {result['message']}"

    if fn_name == "reminder_list":
        reminders = await reminder_list(channel_id)
        if not reminders:
            return "No pending reminders."
        lines = [f"#{r['id']} [{r['remind_at']}] {r['message']}" for r in reminders]
        return "\n".join(lines)

    if fn_name == "reminder_cancel":
        ok = await reminder_cancel(args["reminder_id"])
        return f"Reminder #{args['reminder_id']} cancelled." if ok else "Reminder not found or already fired."

    return f"Unknown reminder tool: {fn_name}"
