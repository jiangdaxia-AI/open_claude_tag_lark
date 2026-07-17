"""Tool handlers — adapt existing dispatch functions to the ToolHandler protocol.

Each handler wraps an existing dispatch function (dispatch_task_tool,
dispatch_reminder_tool, etc.) and adapts it to the unified runtime interface.
This allows incremental migration: the underlying dispatch functions stay
unchanged, only the calling code in loop.py changes.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from ocl.agents.cron_store import CRON_TOOL_SCHEMAS, dispatch_cron_tool
from ocl.agents.reminder_store import REMINDER_TOOL_SCHEMAS, dispatch_reminder_tool
from ocl.agents.task_store import TASK_TOOL_SCHEMAS, dispatch_task_tool, task_list_by_session
from ocl.config import settings

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)


class TaskHandler:
    """Handles task_* tools — wraps dispatch_task_tool.

    When task_create assigns to another agent, automatically triggers
    delegation to wake that agent.

    Within a session, sequential task_create calls are chained:
    task N automatically gets depends_on set to task N-1's ID.
    Only the FIRST task's assignee is woken immediately — subsequent
    assignees are woken by trigger_downstream when the previous
    task completes.
    """

    schemas = TASK_TOOL_SCHEMAS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        is_first = True

        # For task_create in a session, chain with the previous task
        if name == "task_create" and rt.session_id:
            is_first = await self._chain_previous_task(rt, args)

        result = await dispatch_task_tool(
            name,
            args,
            channel_id=rt.channel_id,
            agent_id=rt.agent_id,
            session_id=rt.session_id,
        )

        # Wake the assignee ONLY for the first task in the chain.
        # Chained tasks are woken by trigger_downstream when their
        # dependency completes.
        if name == "task_create" and args.get("assignee") and is_first:
            await self._maybe_wake_assignee(rt, args["assignee"], result)

        # When a task is marked done or closed, record task-layer memory
        # and trigger downstream dependent tasks
        if name == "task_update" and args.get("status") in ("done", "closed"):
            await self._record_task_memory(rt, args["task_id"], args.get("result", ""))
            await self._trigger_downstream_tasks(rt, args["task_id"])

        return result

    async def _record_task_memory(
        self, rt: "AgentRuntime", task_id: int, result: str
    ) -> None:
        """Write a completed task's outcome to the task-layer memory.

        Task memory is scoped to (channel, agent, session) so future turns
        in the same big-task session can recall what was accomplished —
        without re-reading every artifact or task record.
        """
        if not settings.memory_layered_enabled:
            return
        try:
            from ocl.agents.task_store import task_get, task_list_by_session
            from ocl.memory.layered import get_layered_memory

            task = await task_get(rt.channel_id, task_id)
            if not task:
                return
            session_id = task.get("session_id") or rt.session_id
            if not session_id:
                return

            # Rebuild the session's completed-task digest and commit it as
            # one task-scope file (segment reconciliation keeps it cheap).
            tasks = await task_list_by_session(rt.channel_id, session_id)
            done = [t for t in tasks if t.get("status") in ("done", "closed")]
            lines = [
                f"- 任务 #{t['id']} [{t.get('assignee') or 'self'}] {t['title']}"
                + (f" → {str(t.get('result'))[:200]}" if t.get("result") else "")
                for t in done
            ]
            content = "# 任务进展\n" + "\n".join(lines)

            await get_layered_memory().commit(
                rt.channel_id,
                rt.agent_id,
                name="task-progress",
                content=content,
                scope="task",
                session_id=session_id,
                description="Completed tasks and outcomes in this session",
                priority="P2",
            )
        except Exception:
            logger.exception("Failed to record task memory (task_id=%d)", task_id)

    async def _trigger_downstream_tasks(
        self, rt: "AgentRuntime", completed_task_id: int
    ) -> None:
        """When a task completes, wake agents for dependent tasks."""
        from ocl.runtime.delegation import trigger_downstream

        try:
            await trigger_downstream(
                gateway=rt.gateway,
                channel_id=rt.channel_id,
                completed_task_id=completed_task_id,
                completed_agent_id=rt.agent_id,
                _depth=rt.delegation_depth,
                _upstream_agents=rt.upstream_agents,
            )
        except Exception:
            logger.exception(
                "Failed to trigger downstream after task #%d", completed_task_id
            )

    async def _chain_previous_task(self, rt: "AgentRuntime", args: dict) -> bool:
        """Chain with the previous task in this session.

        Sets args["depends_on"] to the previous task's ID so the new task
        only starts after the previous one completes (via trigger_downstream).

        Returns True if this is the first task in the chain (no dependency).
        Returns False if a dependency was set (task is chained).
        """
        try:
            existing = await task_list_by_session(rt.channel_id, rt.session_id)
        except Exception:
            return True

        if not existing:
            return True  # First task in session

        # Find the most recent active task (not done/closed)
        active = [t for t in existing if t.get("status") not in ("done", "closed")]
        if not active:
            return True  # All previous tasks done — start fresh

        # Chain: set depends_on to the most recent active task
        prev_id = str(active[-1]["id"])
        existing_deps = args.get("depends_on", "")
        if existing_deps:
            args["depends_on"] = f"{existing_deps},{prev_id}"
        else:
            args["depends_on"] = prev_id

        logger.info(
            "Chained task '%s' depends on task #%s (session=%s)",
            args.get("title", "?"), prev_id, rt.session_id,
        )
        return False

    async def _maybe_wake_assignee(
        self, rt: "AgentRuntime", assignee: str, create_result: str
    ) -> None:
        """If the assignee matches another agent, wake them to handle the task."""
        import asyncio

        from ocl.agents.config import load_agents
        from ocl.gateway.router import route_message

        # Skip if assignee is empty, "self", or the current agent
        assignee = assignee.strip().lstrip("@")
        if not assignee or assignee == "self" or assignee == rt.agent_id:
            return

        try:
            registry = load_agents(rt.channel_id)
        except Exception:
            return

        # Find matching agent by display_name or agent_id
        target_cfg = None
        for cfg in registry.iter_enabled():
            if cfg.agent_id == rt.agent_id:
                continue
            if cfg.display_name == assignee or cfg.agent_id == assignee:
                target_cfg = cfg
                break
            # Also try partial match (e.g. "产品" matches "产品专家")
            if assignee in cfg.display_name or assignee in cfg.agent_id:
                target_cfg = cfg
                break

        if not target_cfg or not target_cfg.feishu_bot_open_id:
            return

        # Extract task info from result string (e.g. "Task #3 created: ...")
        task_title = ""
        for line in create_result.split("\n"):
            if line.startswith("Task #"):
                task_title = line.split("created: ", 1)[-1] if "created: " in line else ""
                break

        task_msg = (
            f"我是 @{rt.agent_id}。分配给你的任务：\n\n"
            f"**{task_title}**\n\n"
            f"请你自己执行（不要再次分派）。"
            f"完成后调用 task_update(status='done', task_id=<任务ID>, result=你的产出摘要)。"
        )

        try:
            # Send a visible @mention in the channel so the user sees the delegation
            if hasattr(rt.gateway, "send_message_with_mentions"):
                await rt.gateway.send_message_with_mentions(
                    chat_id=rt.channel_id,
                    text=f"@{target_cfg.display_name} {task_title}\n请接手处理这个任务。",
                    mentions=[{"open_id": target_cfg.feishu_bot_open_id, "name": target_cfg.display_name}],
                    agent_id=rt.agent_id,
                )
        except Exception:
            logger.exception("Failed to send @mention to agent %s", target_cfg.agent_id)

        try:
            # Wake the target agent via route_message (non-blocking)
            asyncio.create_task(route_message(
                gateway=rt.gateway,
                tenant_id=rt.gateway.tenant_id,
                chat_id=rt.channel_id,
                user_id=f"agent:{rt.agent_id}",
                text=task_msg,
                message_id=f"task_assign_{__import__('time').time()}",
                agent_id=target_cfg.agent_id,
                _delegation_depth=rt.delegation_depth + 1,
                _upstream_agents=rt.upstream_agents | {rt.agent_id},
                _session_id=rt.session_id,
            ))
        except Exception:
            logger.exception("Failed to wake agent %s for task assignment", target_cfg.agent_id)


class ReminderHandler:
    """Handles reminder_* tools — wraps dispatch_reminder_tool."""

    schemas = REMINDER_TOOL_SCHEMAS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        return await dispatch_reminder_tool(
            name,
            args,
            channel_id=rt.channel_id,
            agent_id=rt.agent_id,
        )


class CronHandler:
    """Handles schedule_task, list_crons, cancel_cron — wraps dispatch_cron_tool."""

    schemas = CRON_TOOL_SCHEMAS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        return await dispatch_cron_tool(
            rt.channel_id,
            rt.agent_id,
            name,
            args,
        )


class MemoryHandler:
    """Handles memory_append, memory_replace, memory_delete.

    Migrated from loop.py's _handle_memory_tool. Manages MEMORY.md files.
    After each write, syncs MEMORY.md into the layered memory store
    (segment reconciliation makes re-commits nearly free).
    """

    schemas: list[dict] = []  # Schemas live in BUILTIN_TOOLS, not duplicated here

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        _handle_memory_tool(rt.channel_id, name, args, agent_id=rt.agent_id)
        await sync_memory_to_layered(rt.channel_id, rt.agent_id)
        return "Memory updated."


async def sync_memory_to_layered(channel_id: str, agent_id: str) -> None:
    """Re-commit an agent's MEMORY.md into the layered store (global scope).

    The layered store reconciles per-line segments, so only changed lines
    are re-embedded. Best-effort — failures never block the agent loop.
    """
    if not settings.memory_layered_enabled:
        return
    try:
        if agent_id != "default":
            memory_path = settings.channels_dir / channel_id / "agents" / agent_id / "MEMORY.md"
        else:
            memory_path = settings.channels_dir / channel_id / "MEMORY.md"
        content = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

        from ocl.memory.layered import get_layered_memory

        await get_layered_memory().commit(
            channel_id,
            agent_id,
            name="agent-memory",
            content=content,
            scope="global",
            description=f"Long-term memory of agent {agent_id}",
        )
    except Exception:
        logger.exception("Failed to sync MEMORY.md to layered store (agent=%s)", agent_id)


def _handle_memory_tool(
    channel_id: str, fn_name: str, args: dict[str, Any], agent_id: str = "default"
) -> None:
    """Manage MEMORY.md — migrated from loop.py.

    Per-agent memory: channels/<channel_id>/agents/<agent_id>/MEMORY.md
    Channel-level memory (default agent): channels/<channel_id>/MEMORY.md
    """
    if agent_id != "default":
        memory_path = settings.channels_dir / channel_id / "agents" / agent_id / "MEMORY.md"
    else:
        memory_path = settings.channels_dir / channel_id / "MEMORY.md"

    memory_path.parent.mkdir(parents=True, exist_ok=True)
    current = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    if fn_name == "memory_append":
        entry = args.get("content", "").strip()
        priority = args.get("priority", "P2")
        if priority not in ("P1", "P2", "P3"):
            priority = "P2"
        if entry:
            today = date.today().isoformat()
            line = f"- [{today}] [{priority}] {entry}"
            memory_path.write_text(
                current.rstrip() + f"\n{line}\n", encoding="utf-8"
            )

    elif fn_name == "memory_replace":
        old = args.get("old", "")
        new = args.get("new", "")
        memory_path.write_text(current.replace(old, new), encoding="utf-8")

    elif fn_name == "memory_delete":
        target = args.get("content", "").strip()
        if target:
            lines = current.splitlines(keepends=True)
            filtered = [ln for ln in lines if target not in ln]
            memory_path.write_text("".join(filtered), encoding="utf-8")


class FallbackHandler:
    """Fallback handler — delegates to registry.dispatch_tool.

    Handles: builtins (web_search, save_artifact, etc.), feishu_doc_*,
    mcp__*, search_channel_history.
    """

    schemas: list[dict] = []  # Aggregated by dispatcher.list_tools() via registry

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        from ocl.tools.registry import dispatch_tool

        return await dispatch_tool(
            name,
            args,
            channel_id=rt.channel_id,
            store=rt.store,
            agent_id=rt.agent_id,
            user_id=rt.user_id,
        )
