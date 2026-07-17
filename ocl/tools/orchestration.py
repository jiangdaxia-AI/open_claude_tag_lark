"""Orchestration tools — let the agent autonomously plan and execute subtasks.

Tools exposed to agents:
  - plan_subtasks: Create a batch of subtasks with DAG dependencies
  - run_subtask: Start executing a subtask (self or delegate)
  - wait_subtasks: Wait for subtasks to complete and get results
  - get_subtask_status: Query all subtasks in the current session
  - retry_subtask: Reset a failed subtask for retry
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocl.runtime.orchestrator import Orchestrator

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)

ORCHESTRATION_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "plan_subtasks",
            "description": (
                "Plan subtasks for a complex task. You decide how to break down the task, "
                "define dependencies (DAG), and assign subtasks to yourself or other agents.\n\n"
                "Each subtask can depend on other subtasks by title. Dependencies form a DAG — "
                "a task can depend on multiple prerequisites.\n\n"
                "Example:\n"
                "  [{\"title\": \"search_data\", \"assignee\": \"self\"},\n"
                "   {\"title\": \"clean_data\", \"depends_on\": [\"search_data\"], \"assignee\": \"self\"},\n"
                "   {\"title\": \"write_report\", \"depends_on\": [\"clean_data\"], \"assignee\": \"self\"}]\n\n"
                "Use assignee=\"self\" for tasks you handle, or another agent's ID to delegate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Short task title"},
                                "description": {"type": "string", "description": "Detailed task description"},
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Titles of prerequisite tasks within this plan",
                                },
                                "assignee": {
                                    "type": "string",
                                    "description": "Agent ID or 'self' (default: self)",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_subtask",
            "description": (
                "Start executing a subtask. If assigned to yourself, marks it as in_progress "
                "and returns the task details for you to work on. If assigned to another agent, "
                "delegates to that agent.\n\n"
                "After completing a self-assigned task, call task_update with status='done' "
                "and include the result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The subtask ID to execute"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_subtasks",
            "description": (
                "Wait for subtasks to complete. Returns their results.\n"
                "Use this after starting subtasks that other agents are handling, "
                "or after delegating work. Polls until all specified tasks are done/failed "
                "or timeout (default 300s)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of subtask IDs to wait for",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max wait time in seconds (default: 300)",
                    },
                },
                "required": ["task_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subtask_status",
            "description": "Get the status of all subtasks in the current session.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_subtask",
            "description": (
                "Reset a subtask to 'todo' so it can be retried. "
                "Use this when a subtask's result is not satisfactory and needs to be redone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The subtask ID to retry"},
                    "reason": {"type": "string", "description": "Why retrying (optional)"},
                },
                "required": ["task_id"],
            },
        },
    },
]


class OrchestrationHandler:
    """Handles orchestration tools — plan/run/wait/status/retry.

    Delegates to the Orchestrator engine which manages task DAG,
    execution, and delegation.
    """

    schemas = ORCHESTRATION_TOOL_SCHEMAS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        orchestrator = Orchestrator()

        try:
            if name == "plan_subtasks":
                tasks = args.get("tasks", [])
                if not tasks:
                    return "Error: no tasks provided in plan."
                created = await orchestrator.plan(rt, tasks)
                lines = [f"Created {len(created)} subtasks:"]
                for st in created:
                    deps = f" (depends on: {', '.join(st.depends_on)})" if st.depends_on else ""
                    assignee = st.assignee if st.assignee != rt.agent_id else "self"
                    lines.append(f"  #{st.id} {st.title} [{st.status}] @{assignee}{deps}")
                lines.append("\nCall run_subtask to start executing. Tasks with no dependencies can start immediately.")
                return "\n".join(lines)

            if name == "run_subtask":
                task_id = args.get("task_id", "")
                if not task_id:
                    return "Error: no task_id provided."
                return await orchestrator.run_subtask(rt, task_id)

            if name == "wait_subtasks":
                task_ids = args.get("task_ids", [])
                timeout = args.get("timeout_seconds", 300)
                if not task_ids:
                    return "Error: no task_ids provided."
                results = await orchestrator.wait_for(rt, task_ids, timeout)
                lines = ["Subtask results:"]
                for tid, result in results.items():
                    lines.append(f"  #{tid}: {result}")
                return "\n".join(lines)

            if name == "get_subtask_status":
                subtasks = await orchestrator.get_status(rt)
                if not subtasks:
                    return "No subtasks in current session."
                lines = [f"Subtasks in session ({len(subtasks)}):"]
                for st in subtasks:
                    deps = f" (depends on: {', '.join(st.depends_on)})" if st.depends_on else ""
                    assignee = st.assignee if st.assignee != rt.agent_id else "self"
                    result_preview = f" -> {st.result[:80]}..." if st.result else ""
                    lines.append(f"  #{st.id} {st.title} [{st.status}] @{assignee}{deps}{result_preview}")
                return "\n".join(lines)

            if name == "retry_subtask":
                task_id = args.get("task_id", "")
                reason = args.get("reason", "")
                if not task_id:
                    return "Error: no task_id provided."
                return await orchestrator.retry(rt, task_id, reason)

            return f"Unknown orchestration tool: {name}"

        except Exception as e:
            logger.exception("Orchestration tool %s failed", name)
            return f"Orchestration tool '{name}' failed: {type(e).__name__}: {e}"
