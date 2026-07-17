"""Cron-based scheduled tasks — agent self-registered monitoring jobs.

Agents can call schedule_task(cron, description) to register a recurring
monitoring job. The job fires on the cron schedule and wakes the agent
with the description as the task prompt.

Main agent can list/cancel crons to act as a "supervisor".

Stored in SQLite (crons.db) for persistence across restarts.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_CRONS = """
CREATE TABLE IF NOT EXISTS crons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    cron_expr   TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    last_run    REAL,
    run_count   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_crons_channel ON crons(channel_id, status);
"""

_scheduler: AsyncIOScheduler | None = None


def init_scheduler() -> AsyncIOScheduler:
    """Get or create the global AsyncIOScheduler.

    Must be called from within an async context (event loop running).
    """
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
        _scheduler.start()
    return _scheduler


async def _get_db() -> aiosqlite.Connection:
    db_path = settings.data_dir / "workspaces" / "crons.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_CRONS)
    await db.commit()
    return db


async def schedule_task(
    channel_id: str,
    agent_id: str,
    cron_expr: str,
    description: str,
) -> dict[str, Any]:
    """Register a cron-based scheduled task.

    Args:
        channel_id: Channel to post in when the task fires
        agent_id: Agent to wake when the task fires
        cron_expr: Cron expression (e.g. "0 9 * * 1" = every Monday 9am)
        description: What the agent should do when woken

    Returns: {"id": int, "status": "active"}
    """
    # Validate cron expression
    try:
        trigger = CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        return {"error": f"Invalid cron expression: {e}"}

    db = await _get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO crons (channel_id, agent_id, cron_expr, description)
               VALUES (?, ?, ?, ?)""",
            (channel_id, agent_id, cron_expr, description),
        )
        await db.commit()
        cron_id = cursor.lastrowid

        # Register with APScheduler
        scheduler = init_scheduler()
        scheduler.add_job(
            _fire_cron,
            trigger=trigger,
            args=[cron_id, channel_id, agent_id, description],
            id=f"cron_{cron_id}",
            replace_existing=True,
        )
        logger.info("Cron %d scheduled: %s for agent=%s channel=%s",
                    cron_id, cron_expr, agent_id, channel_id)
        return {"id": cron_id, "status": "active"}
    finally:
        await db.close()


async def list_crons(
    channel_id: str | None = None,
    agent_id: str | None = None,
    status: str = "active",
) -> list[dict[str, Any]]:
    """List scheduled crons, optionally filtered."""
    db = await _get_db()
    try:
        query = "SELECT * FROM crons WHERE status = ?"
        params: list = [status]
        if channel_id:
            query += " AND channel_id = ?"
            params.append(channel_id)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY created_at DESC"
        cursor = await db.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def cancel_cron(cron_id: int) -> dict[str, Any]:
    """Cancel a scheduled cron."""
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE crons SET status = 'cancelled' WHERE id = ?",
            (cron_id,),
        )
        await db.commit()
        # Remove from APScheduler
        scheduler = init_scheduler()
        try:
            scheduler.remove_job(f"cron_{cron_id}")
        except Exception:
            pass  # job may not exist
        return {"id": cron_id, "status": "cancelled"}
    finally:
        await db.close()


async def restore_crons_on_startup() -> int:
    """Re-register all active crons from DB into APScheduler (after restart).

    Returns count of restored crons.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM crons WHERE status = 'active'"
        )
        rows = await cursor.fetchall()
        scheduler = init_scheduler()
        count = 0
        for row in rows:
            try:
                trigger = CronTrigger.from_crontab(row["cron_expr"])
                scheduler.add_job(
                    _fire_cron,
                    trigger=trigger,
                    args=[row["id"], row["channel_id"], row["agent_id"], row["description"]],
                    id=f"cron_{row['id']}",
                    replace_existing=True,
                )
                count += 1
            except Exception as e:
                logger.warning("Failed to restore cron %d: %s", row["id"], e)
        if count:
            logger.info("Restored %d cron jobs from DB", count)
        return count
    finally:
        await db.close()


async def _fire_cron(
    cron_id: int,
    channel_id: str,
    agent_id: str,
    description: str,
) -> None:
    """Called by APScheduler when a cron fires — wakes the agent."""
    import time
    logger.info("Cron %d fired: %s for agent=%s", cron_id, description[:80], agent_id)

    # Update last_run and run_count
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE crons SET last_run = ?, run_count = run_count + 1 WHERE id = ?",
            (time.time(), cron_id),
        )
        await db.commit()
    finally:
        await db.close()

    # Wake the agent via router — the agent will see the description as a task
    try:
        from ocl.gateway.router import route_message
        from ocl.gateway.feishu.ws_client import _get_gateway_if_started

        gateway = _get_gateway_if_started()
        if gateway is None:
            logger.warning("Gateway not started, cannot fire cron %d", cron_id)
            return

        await route_message(
            gateway=gateway,
            tenant_id=gateway.tenant_id,
            chat_id=channel_id,
            user_id=f"cron:{cron_id}",
            text=f"[定时任务] {description}",
            message_id=f"cron_{cron_id}_{int(time.time())}",
            agent_id=agent_id,
        )
    except Exception:
        logger.exception("Failed to fire cron %d", cron_id)


# ── Tool dispatch (called from agent loop) ───────────────────────────────────

CRON_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": (
                "Schedule a recurring monitoring task. The system will wake you "
                "on the cron schedule with the description as a prompt. "
                "Use for periodic checks (e.g. 'check stale PRs every Monday')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cron": {
                        "type": "string",
                        "description": (
                            "Standard 5-field cron expression. "
                            "Examples: '0 9 * * 1' = every Mon 9am, "
                            "'0 */2 * * *' = every 2 hours, "
                            "'30 18 * * 1-5' = weekdays 6:30pm"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "What to do when the task fires",
                    },
                },
                "required": ["cron", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_crons",
            "description": "List all scheduled cron tasks (active and cancelled).",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: active (default) or cancelled",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_cron",
            "description": "Cancel a scheduled cron task by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cron_id": {"type": "integer", "description": "The cron task ID"},
                },
                "required": ["cron_id"],
            },
        },
    },
]


async def dispatch_cron_tool(
    channel_id: str, agent_id: str, fn_name: str, args: dict[str, Any]
) -> str:
    """Dispatch a cron-related tool call. Returns result string for the LLM."""
    if fn_name == "schedule_task":
        result = await schedule_task(
            channel_id=channel_id,
            agent_id=agent_id,
            cron_expr=args.get("cron", ""),
            description=args.get("description", ""),
        )
        if "error" in result:
            return f"Failed: {result['error']}"
        return f"Scheduled task #{result['id']} created. It will fire on cron '{args.get('cron')}' and wake you with: {args.get('description')}"

    elif fn_name == "list_crons":
        status = args.get("status", "active")
        crons = await list_crons(channel_id=channel_id, status=status)
        if not crons:
            return f"No {status} cron tasks."
        lines = []
        for c in crons:
            lines.append(f"#{c['id']} [{c['cron_expr']}] {c['description']} (runs: {c['run_count']}, agent: {c['agent_id']})")
        return "\n".join(lines)

    elif fn_name == "cancel_cron":
        cron_id = int(args.get("cron_id", 0))
        result = await cancel_cron(cron_id)
        return f"Cron #{cron_id} cancelled."

    return f"Unknown cron tool: {fn_name}"
