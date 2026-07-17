"""Orchestrator — AI-driven task planning and execution engine.

The orchestrator provides tools that let the agent autonomously decide how to
break down complex tasks, define DAG dependencies, execute subtasks, wait for
completion, and retry failures.

Key design: orchestration is a TOOL, not a framework. The agent calls
plan_subtasks / run_subtask / wait_subtasks to self-organize. The system
provides the execution engine, not the planning logic.

SubTask states map to task_store states: todo -> in_progress -> done / closed.
DAG support: a task can depend on multiple prerequisites (depends_on list).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ocl.agents.task_store import task_create, task_get, task_update, task_list

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """A subtask in an orchestration plan."""

    id: str
    title: str
    description: str
    assignee: str  # agent_id or "self"
    depends_on: list[str]  # prerequisite task IDs
    status: str  # todo / in_progress / done / closed (maps to task_store states)
    result: str = ""

    @classmethod
    def from_task_dict(cls, task: dict[str, Any]) -> "SubTask":
        """Build a SubTask from a task_store row."""
        depends_raw = task.get("depends_on", "")
        depends_on = [s.strip() for s in depends_raw.split(",") if s.strip()] if depends_raw else []
        return cls(
            id=str(task["id"]),
            title=task.get("title", ""),
            description=task.get("description", ""),
            assignee=task.get("assignee", "") or "self",
            depends_on=depends_on,
            status=task.get("status", "todo"),
            result=task.get("result", ""),
        )


class Orchestrator:
    """Agent-driven orchestration engine.

    All methods take an AgentRuntime for context (channel_id, agent_id, session_id).
    Tasks are stored in the existing task_store SQLite DB.
    """

    async def plan(
        self,
        rt: "AgentRuntime",
        tasks: list[dict[str, Any]],
    ) -> list[SubTask]:
        """Create a batch of subtasks with DAG dependencies.

        Args:
            tasks: list of task specs, each with:
                - title (required)
                - description (optional)
                - depends_on: list of task titles (resolved to IDs after creation)
                - assignee: agent_id or "self" (default: "self")

        Returns: list of created SubTask objects.

        Dependency resolution: depends_on uses task titles within this batch.
        We create tasks in order, resolving title -> ID as we go.
        """
        title_to_id: dict[str, str] = {}
        created: list[SubTask] = []

        for spec in tasks:
            title = spec.get("title", "").strip()
            if not title:
                continue

            description = spec.get("description", "")
            assignee = spec.get("assignee", "self")
            if assignee == "self":
                assignee = rt.agent_id

            # Resolve depends_on: titles -> IDs
            dep_titles = spec.get("depends_on", [])
            dep_ids: list[str] = []
            for dep_title in dep_titles:
                dep_title = dep_title.strip()
                if dep_title in title_to_id:
                    dep_ids.append(title_to_id[dep_title])
                else:
                    logger.warning("Dependency '%s' not found in plan, skipping", dep_title)

            depends_on_str = ",".join(dep_ids)

            task = await task_create(
                channel_id=rt.channel_id,
                creator=rt.agent_id,
                title=title,
                description=description,
                assignee=assignee,
                priority="P2",
                depends_on=depends_on_str,
                session_id=rt.session_id,
                parent_session_id=rt.session_id,
            )

            subtask = SubTask.from_task_dict(task)
            title_to_id[title] = subtask.id
            created.append(subtask)

        return created

    async def run_subtask(
        self,
        rt: "AgentRuntime",
        task_id: str,
    ) -> str:
        """Start executing a subtask.

        If assignee is self (the current agent), marks the task as in_progress
        and returns a prompt for the agent to work on it.

        If assignee is another agent, delegates to that agent via the
        delegation mechanism (fire-and-forget).
        """
        try:
            tid = int(task_id)
        except ValueError:
            return f"Invalid task ID: {task_id}"

        task = await task_get(rt.channel_id, tid)
        if not task:
            return f"Task #{task_id} not found."

        if task["status"] not in ("todo",):
            return f"Task #{task_id} is already {task['status']}, cannot start."

        assignee = task.get("assignee", "") or rt.agent_id

        # Mark as in_progress
        await task_update(rt.channel_id, tid, status="in_progress")

        if assignee == rt.agent_id or not assignee:
            # Self-execution: agent works on it in the current conversation
            desc = task.get("description", "")
            deps_info = ""
            depends_raw = task.get("depends_on", "")
            if depends_raw:
                deps_info = f"\nDependencies: {depends_raw}"

            return (
                f"Task #{task_id} started (self-execution).\n"
                f"Title: {task['title']}\n"
                f"Description: {desc}{deps_info}\n"
                f"Work on this task now. When done, call task_update with status='done' and result."
            )
        else:
            # Delegate to another agent
            return await self._delegate_subtask(rt, task, assignee)

    async def _delegate_subtask(
        self,
        rt: "AgentRuntime",
        task: dict[str, Any],
        target_agent_id: str,
    ) -> str:
        """Delegate a subtask to another agent via the delegation mechanism."""
        from ocl.agents.config import load_agents

        try:
            registry = load_agents(rt.channel_id)
            target_cfg = registry.get(target_agent_id)
            if not target_cfg or not target_cfg.feishu_bot_open_id:
                return (
                    f"Task #{task['id']} assigned to agent '{target_agent_id}' "
                    f"but that agent has no bot_open_id. Cannot delegate."
                )
        except Exception:
            return f"Failed to load agent config for '{target_agent_id}'."

        # Use the delegation mechanism to wake the target agent
        import asyncio as _asyncio

        task_msg = (
            f"Task #{task['id']}: {task['title']}\n"
            f"Description: {task.get('description', '')}\n"
            f"Please handle this task. When done, update task #{task['id']} to 'done'."
        )

        _asyncio.create_task(_wake_agent(
            gateway=rt.gateway,
            channel_id=rt.channel_id,
            current_agent_id=rt.agent_id,
            target_agent_id=target_agent_id,
            task_msg=task_msg,
            task_id=task["id"],
            depth=rt.delegation_depth + 1,
            upstream=rt.upstream_agents | {rt.agent_id},
        ))

        return f"Task #{task['id']} delegated to agent '{target_agent_id}'. It will be processed asynchronously."

    async def wait_for(
        self,
        rt: "AgentRuntime",
        task_ids: list[str],
        timeout_seconds: int = 300,
    ) -> dict[str, str]:
        """Wait for subtasks to complete. Returns {task_id: result}.

        Polls task status until all are done/failed or timeout.
        """
        start = time.time()
        results: dict[str, str] = {}
        pending_ids = set(str(tid) for tid in task_ids)

        while pending_ids and time.time() - start < timeout_seconds:
            for tid in list(pending_ids):
                try:
                    task = await task_get(rt.channel_id, int(tid))
                except (ValueError, Exception):
                    results[tid] = f"Invalid task ID: {tid}"
                    pending_ids.discard(tid)
                    continue

                if not task:
                    results[tid] = f"Task #{tid} not found"
                    pending_ids.discard(tid)
                    continue

                status = task.get("status", "")
                if status in ("done", "closed"):
                    results[tid] = task.get("result", "") or "(no result)"
                    pending_ids.discard(tid)
                # Note: task_store doesn't have a 'failed' state.
                # If a task needs retry, the agent calls retry_subtask which
                # resets it to 'todo'. wait_for will then continue polling
                # until it becomes 'done' or times out.

            if pending_ids:
                await asyncio.sleep(2)  # poll interval

        # Timeout: report remaining as still pending
        for tid in pending_ids:
            results[tid] = f"Timeout: task #{tid} still running after {timeout_seconds}s"

        return results

    async def get_status(self, rt: "AgentRuntime") -> list[SubTask]:
        """Get all subtasks for the current session (all statuses)."""
        tasks = await task_list(
            channel_id=rt.channel_id,
            status="all",
            session_id=rt.session_id or None,
        )
        return [SubTask.from_task_dict(t) for t in tasks]

    async def retry(
        self,
        rt: "AgentRuntime",
        task_id: str,
        reason: str = "",
    ) -> str:
        """Reset a subtask for retry. Resets status to todo.

        Works for any non-done task that the agent wants to redo.
        """
        try:
            tid = int(task_id)
        except ValueError:
            return f"Invalid task ID: {task_id}"

        task = await task_get(rt.channel_id, tid)
        if not task:
            return f"Task #{task_id} not found."

        # Reset to todo for retry
        await task_update(
            rt.channel_id, tid, status="todo",
            result=f"Retry reason: {reason}" if reason else None,
        )

        return (
            f"Task #{task_id} reset to 'todo' for retry."
            f"{' Reason: ' + reason if reason else ''}"
            f" Call run_subtask to start it again."
        )


async def _wake_agent(
    gateway,
    channel_id: str,
    current_agent_id: str,
    target_agent_id: str,
    task_msg: str,
    task_id: int,
    depth: int,
    upstream: set[str],
) -> None:
    """Wake a target agent to handle a delegated subtask."""
    import time as _time

    from ocl.gateway.router import route_message as _route_message

    try:
        await _route_message(
            gateway=gateway,
            tenant_id=gateway.tenant_id,
            chat_id=channel_id,
            user_id=f"agent:{current_agent_id}",
            text=task_msg,
            message_id=f"orchestrate_{_time.time()}_{task_id}",
            agent_id=target_agent_id,
            _delegation_depth=depth,
            _upstream_agents=upstream,
            _delegation_task_id=task_id,
        )
    except Exception:
        logger.exception("Failed to wake agent %s for task %d", target_agent_id, task_id)
