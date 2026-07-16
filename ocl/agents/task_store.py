"""Task system: SQLite-backed kanban with 5-state workflow.

States: todo → in_progress → in_review → done → closed

Tasks are scoped per channel (not per agent) — any agent or human can create,
claim, update, or list tasks. Task IDs are auto-incremented per DB.

Tool interface (exposed to agents via dispatch_task_tool):
  task_create(title, description?, assignee?, priority?)
  task_claim(task_id)
  task_assign(task_id, assignee)
  task_update(task_id, status, description?)
  task_list(status?, assignee?)
  task_get(task_id)
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from ocl.config import settings

logger = logging.getLogger(__name__)

_VALID_STATUSES = ("todo", "in_progress", "in_review", "done", "closed")
_VALID_PRIORITIES = ("P1", "P2", "P3")

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'todo',
    assignee    TEXT DEFAULT '',
    creator     TEXT NOT NULL,
    priority    TEXT DEFAULT 'P2',
    message_id  TEXT DEFAULT '',
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    closed_at   REAL
);
"""

_CREATE_TASKS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tasks_channel_status
    ON tasks(channel_id, status);
"""


async def _get_task_db() -> aiosqlite.Connection:
    """Get or create a DB connection for task storage.

    Tasks use a separate SQLite DB (tasks.db) to avoid schema coupling
    with the message store.
    """
    db_path = settings.data_dir / "workspaces" / "tasks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_TASKS + _CREATE_TASKS_INDEX)
    await db.commit()
    return db


# ── Core CRUD ──


async def task_create(
    channel_id: str,
    creator: str,
    title: str,
    description: str = "",
    assignee: str = "",
    priority: str = "P2",
    message_id: str = "",
) -> dict[str, Any]:
    """Create a new task. Returns the task dict."""
    if priority not in _VALID_PRIORITIES:
        priority = "P2"

    db = await _get_task_db()
    try:
        cursor = await db.execute(
            """INSERT INTO tasks (channel_id, title, description, assignee, creator, priority, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, title, description, assignee, creator, priority, message_id),
        )
        await db.commit()
        task_id = cursor.lastrowid
        return await task_get(channel_id, task_id)  # type: ignore[return-value]
    finally:
        await db.close()


async def task_claim(channel_id: str, agent_id: str, task_id: int) -> dict[str, Any] | None:
    """Claim a task: set status to in_progress and assignee to agent_id."""
    db = await _get_task_db()
    try:
        await db.execute(
            """UPDATE tasks SET assignee = ?, status = 'in_progress',
               updated_at = unixepoch('now','subsec')
               WHERE id = ? AND channel_id = ? AND status IN ('todo', 'in_progress')""",
            (agent_id, task_id, channel_id),
        )
        await db.commit()
        return await task_get(channel_id, task_id)
    finally:
        await db.close()


async def task_assign(
    channel_id: str, task_id: int, assignee: str
) -> dict[str, Any] | None:
    """Assign a task to someone (agent or human)."""
    db = await _get_task_db()
    try:
        await db.execute(
            """UPDATE tasks SET assignee = ?, updated_at = unixepoch('now','subsec')
               WHERE id = ? AND channel_id = ?""",
            (assignee, task_id, channel_id),
        )
        await db.commit()
        return await task_get(channel_id, task_id)
    finally:
        await db.close()


async def task_update(
    channel_id: str,
    task_id: int,
    status: str,
    description: str | None = None,
) -> dict[str, Any] | None:
    """Update task status. Optionally update description."""
    if status not in _VALID_STATUSES:
        return None

    closed_clause = ", closed_at = unixepoch('now','subsec')" if status == "closed" else ""
    desc_clause = ", description = ?" if description is not None else ""
    params: list[Any] = [status]
    if description is not None:
        params.append(description)
    params.extend([task_id, channel_id])

    db = await _get_task_db()
    try:
        await db.execute(
            f"""UPDATE tasks SET status = ?, updated_at = unixepoch('now','subsec'){closed_clause}{desc_clause}
               WHERE id = ? AND channel_id = ?""",
            params,
        )
        await db.commit()
        return await task_get(channel_id, task_id)
    finally:
        await db.close()


async def task_list(
    channel_id: str,
    status: str | None = None,
    assignee: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks in a channel, optionally filtered by status/assignee."""
    db = await _get_task_db()
    try:
        query = "SELECT * FROM tasks WHERE channel_id = ?"
        params: list[Any] = [channel_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        query += " ORDER BY CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END, created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def task_get(channel_id: str, task_id: int) -> dict[str, Any] | None:
    """Get a single task by ID."""
    db = await _get_task_db()
    try:
        async with db.execute(
            "SELECT * FROM tasks WHERE id = ? AND channel_id = ?",
            (task_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


# ── LiteLLM-compatible tool schemas ──

TASK_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a new task in this channel's task board",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title (concise)"},
                    "description": {"type": "string", "description": "Detailed description"},
                    "assignee": {"type": "string", "description": "Who to assign to (agent_id or @username)"},
                    "priority": {"type": "string", "enum": ["P1", "P2", "P3"], "description": "P1=urgent, P2=normal (default), P3=low"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_claim",
            "description": "Claim a task for yourself (sets status to in_progress)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The task ID to claim"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_assign",
            "description": "Assign a task to someone else (agent or human)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The task ID"},
                    "assignee": {"type": "string", "description": "agent_id or @username to assign to"},
                },
                "required": ["task_id", "assignee"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update task status or description",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The task ID"},
                    "status": {
                        "type": "string",
                        "enum": ["todo", "in_progress", "in_review", "done", "closed"],
                        "description": "New status",
                    },
                    "description": {"type": "string", "description": "Updated description (optional)"},
                },
                "required": ["task_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List tasks in this channel",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["todo", "in_progress", "in_review", "done", "closed"],
                        "description": "Filter by status (optional)",
                    },
                    "assignee": {"type": "string", "description": "Filter by assignee (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_get",
            "description": "Get details of a specific task",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The task ID"},
                },
                "required": ["task_id"],
            },
        },
    },
]

_STATUS_EMOJI = {
    "todo": "⬜", "in_progress": "🔄", "in_review": "👀", "done": "✅", "closed": "🏁",
}


async def dispatch_task_tool(
    fn_name: str, args: dict[str, Any], channel_id: str, agent_id: str
) -> Any:
    """Dispatch a task tool call. Returns the result string."""
    if fn_name == "task_create":
        result = await task_create(
            channel_id=channel_id,
            creator=agent_id,
            title=args["title"],
            description=args.get("description", ""),
            assignee=args.get("assignee", ""),
            priority=args.get("priority", "P2"),
        )
        return f"Task #{result['id']} created: {result['title']}\nStatus: {result['status']}"

    if fn_name == "task_claim":
        result = await task_claim(channel_id, agent_id, args["task_id"])
        if result:
            return f"Claimed task #{result['id']}: {result['title']}"
        return f"Task #{args['task_id']} not found or not claimable"

    if fn_name == "task_assign":
        result = await task_assign(channel_id, args["task_id"], args["assignee"])
        if result:
            return f"Task #{result['id']} assigned to {result['assignee']}"
        return f"Task #{args['task_id']} not found"

    if fn_name == "task_update":
        result = await task_update(
            channel_id, args["task_id"], args["status"], args.get("description"),
        )
        if result:
            return f"Task #{result['id']} updated: status={result['status']}"
        return f"Task #{args['task_id']} not found or invalid status"

    if fn_name == "task_list":
        tasks = await task_list(channel_id, args.get("status"), args.get("assignee"))
        if not tasks:
            return "No tasks found."
        lines = []
        for t in tasks:
            lines.append(
                f"{_STATUS_EMOJI.get(t['status'], '❓')} #{t['id']} {t['title']} "
                f"[{t['status']}] @{t['assignee'] or 'unassigned'} ({t['priority']})"
            )
        return "\n".join(lines)

    if fn_name == "task_get":
        result = await task_get(channel_id, args["task_id"])
        if result:
            return (
                f"Task #{result['id']}: {result['title']}\n"
                f"Status: {result['status']}\n"
                f"Assignee: {result['assignee'] or 'unassigned'}\n"
                f"Priority: {result['priority']}\n"
                f"Creator: {result['creator']}\n"
                f"Description: {result['description'] or '(none)'}"
            )
        return f"Task #{args['task_id']} not found"

    return f"Unknown task tool: {fn_name}"
