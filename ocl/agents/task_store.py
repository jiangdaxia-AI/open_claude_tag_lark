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
    depends_on  TEXT DEFAULT '',              -- comma-separated task IDs for dependency chain
    session_id  TEXT DEFAULT '',              -- task session grouping (per user conversation topic)
    result      TEXT DEFAULT '',              -- task result/output (for orchestration)
    parent_session_id TEXT DEFAULT '',        -- parent session for orchestration subtasks
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    closed_at   REAL
);
"""

# Migration: add depends_on column if missing (for existing DBs)
_MIGRATE_ADD_DEPENDS_ON = """
ALTER TABLE tasks ADD COLUMN depends_on TEXT DEFAULT '';
"""

_MIGRATE_ADD_SESSION_ID = """
ALTER TABLE tasks ADD COLUMN session_id TEXT DEFAULT '';
"""

_MIGRATE_ADD_RESULT = """
ALTER TABLE tasks ADD COLUMN result TEXT DEFAULT '';
"""

_MIGRATE_ADD_PARENT_SESSION_ID = """
ALTER TABLE tasks ADD COLUMN parent_session_id TEXT DEFAULT '';
"""

_CREATE_TASKS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tasks_channel_status
    ON tasks(channel_id, status);
"""


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """Add columns if they don't exist (backward compat)."""
    cursor = await db.execute("PRAGMA table_info(tasks)")
    cols = [r[1] for r in await cursor.fetchall()]
    if "depends_on" not in cols:
        await db.execute(_MIGRATE_ADD_DEPENDS_ON)
    if "session_id" not in cols:
        await db.execute(_MIGRATE_ADD_SESSION_ID)
    if "result" not in cols:
        await db.execute(_MIGRATE_ADD_RESULT)
    if "parent_session_id" not in cols:
        await db.execute(_MIGRATE_ADD_PARENT_SESSION_ID)
    await db.commit()


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
    await _migrate_schema(db)
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
    depends_on: str = "",
    session_id: str = "",
    parent_session_id: str = "",
) -> dict[str, Any]:
    """Create a new task. Returns the task dict.

    Args:
        depends_on: comma-separated task IDs this task depends on
                    (used by delegation graph for event-driven chaining)
        parent_session_id: parent session ID for orchestration subtasks
                           (distinguishes orchestration subtasks from user tasks)
    """
    if priority not in _VALID_PRIORITIES:
        priority = "P2"

    db = await _get_task_db()
    try:
        cursor = await db.execute(
            """INSERT INTO tasks (channel_id, title, description, assignee, creator, priority, message_id, depends_on, session_id, parent_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, title, description, assignee, creator, priority, message_id, depends_on, session_id, parent_session_id),
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
    result: str | None = None,
) -> dict[str, Any] | None:
    """Update task status. Optionally update description and result."""
    if status not in _VALID_STATUSES:
        return None

    closed_clause = ", closed_at = unixepoch('now','subsec')" if status == "closed" else ""
    desc_clause = ", description = ?" if description is not None else ""
    result_clause = ", result = ?" if result is not None else ""
    params: list[Any] = [status]
    if description is not None:
        params.append(description)
    if result is not None:
        params.append(result)
    params.extend([task_id, channel_id])

    db = await _get_task_db()
    try:
        await db.execute(
            f"""UPDATE tasks SET status = ?, updated_at = unixepoch('now','subsec'){closed_clause}{desc_clause}{result_clause}
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
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks in a channel, optionally filtered by status/assignee/session.

    Status filtering:
    - 'active' (default when status=None): todo + in_progress + in_review
      — hides completed/historical tasks so agents focus on current work
    - 'all': every status including done/closed (for history queries)
    - any specific status: exact match
    If session_id is provided, only returns tasks from that session.
    """
    db = await _get_task_db()
    try:
        query = "SELECT * FROM tasks WHERE channel_id = ?"
        params: list[Any] = [channel_id]
        if status == "all":
            pass  # No status filter
        elif status and status != "active":
            query += " AND status = ?"
            params.append(status)
        else:
            # Default / 'active': only live tasks
            query += " AND status IN ('todo', 'in_progress', 'in_review')"
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
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
            "description": "Update task status, description, or result",
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
                    "result": {"type": "string", "description": "Task result/output — set this when marking a task as 'done' so callers can retrieve the output via wait_subtasks or get_subtask_status"},
                },
                "required": ["task_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": (
                "List tasks in this channel. By default shows only ACTIVE tasks "
                "(todo/in_progress/in_review) — completed and historical tasks are hidden. "
                "Use status='all' to see everything including done/closed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "todo", "in_progress", "in_review", "done", "closed", "all"],
                        "description": "Filter by status (default 'active' — only live tasks; 'all' for full history)",
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
    fn_name: str, args: dict[str, Any], channel_id: str, agent_id: str,
    session_id: str = "",
) -> Any:
    """Dispatch a task tool call. Returns the result string."""
    if fn_name == "task_create":
        # Resolve assignee: if it matches an agent's display_name or agent_id,
        # normalize to agent_id so permission checks work correctly.
        assignee = args.get("assignee", "").strip().lstrip("@")
        if assignee:
            from ocl.agents.config import load_agents
            try:
                registry = load_agents(channel_id)
                for cfg in registry.iter_enabled():
                    if cfg.display_name == assignee or cfg.agent_id == assignee:
                        assignee = cfg.agent_id
                        break
            except Exception:
                pass  # Keep original assignee if resolution fails

        result = await task_create(
            channel_id=channel_id,
            creator=agent_id,
            title=args["title"],
            description=args.get("description", ""),
            assignee=assignee,
            priority=args.get("priority", "P2"),
            depends_on=args.get("depends_on", ""),
            session_id=session_id,
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
        # Permission check: sub-agents can only update tasks assigned to them.
        # The main/default agent (assistant) has full access as coordinator.
        if agent_id not in ("assistant", "default"):
            existing = await task_get(channel_id, args["task_id"])
            if existing:
                new_status = args.get("status", "")
                # Sub-agents can only close their own tasks
                if new_status in ("closed",) and existing.get("assignee") != agent_id:
                    return f"⚠️ 无权限：任务 #{args['task_id']} 不是分配给你的，不能关闭。你只能操作分配给你的任务。"
                # Sub-agents can only update tasks assigned to them
                if existing.get("assignee") and existing.get("assignee") != agent_id:
                    return f"⚠️ 无权限：任务 #{args['task_id']} 分配给 @{existing.get('assignee')}，不是你的任务。"

        result = await task_update(
            channel_id, args["task_id"], args["status"], args.get("description"), args.get("result"),
        )
        if result:
            return f"Task #{result['id']} updated: status={result['status']}"
        return f"Task #{args['task_id']} not found or invalid status"

    if fn_name == "task_list":
        tasks = await task_list(channel_id, args.get("status"), args.get("assignee"), session_id=session_id or None)
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


# ── Task session management ──────────────────────────────────────────────────

import uuid as _uuid


def new_session_id() -> str:
    """Generate a new task session ID."""
    return _uuid.uuid4().hex[:12]


async def task_list_by_session(channel_id: str, session_id: str) -> list[dict[str, Any]]:
    """List tasks for a specific session only."""
    db = await _get_task_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE channel_id = ? AND session_id = ? ORDER BY id",
            (channel_id, session_id),
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def archive_session(channel_id: str, session_id: str) -> int:
    """Archive all tasks in a session (set status to 'closed')."""
    db = await _get_task_db()
    try:
        cursor = await db.execute(
            """UPDATE tasks SET status = 'closed', closed_at = unixepoch('now','subsec')
               WHERE channel_id = ? AND session_id = ? AND status NOT IN ('closed')""",
            (channel_id, session_id),
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


# ── Delegation graph helpers ─────────────────────────────────────────────────


async def find_dependent_tasks(channel_id: str, completed_task_id: int) -> list[dict[str, Any]]:
    """Find tasks that depend on the given task (for event-driven chaining).

    Returns tasks whose `depends_on` contains the completed_task_id
    and are still in 'todo' status (ready to be triggered).
    """
    db = await _get_task_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM tasks
               WHERE channel_id = ? AND status = 'todo'
               AND (',' || depends_on || ',') LIKE ?""",
            (channel_id, f"%,{completed_task_id},%"),
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


def format_task_board(tasks: list[dict[str, Any]]) -> str:
    """Format tasks as a compact board string for card display."""
    if not tasks:
        return "📋 暂无任务"

    # Group by status
    by_status: dict[str, list] = {"todo": [], "in_progress": [], "in_review": [], "done": [], "closed": []}
    for t in tasks:
        status = t.get("status", "todo")
        if status in by_status:
            by_status[status].append(t)

    status_labels = {
        "todo": "📝 待办",
        "in_progress": "🔄 进行中",
        "in_review": "👀 评审",
        "done": "✅ 完成",
        "closed": "📦 关闭",
    }

    lines = ["📋 任务看板", ""]
    for status, label in status_labels.items():
        items = by_status[status]
        if not items:
            continue
        lines.append(f"{label} ({len(items)})")
        for t in items:
            assignee = t.get("assignee") or "未分配"
            lines.append(f"  #{t['id']} {t['title']} @{assignee}")
        lines.append("")

    return "\n".join(lines)

