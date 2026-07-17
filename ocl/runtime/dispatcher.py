"""ToolDispatcher — unified tool dispatch replacing loop.py's if/elif chain.

Matching priority: exact name > prefix > fallback.

Handlers are registered with either a prefix (e.g. 'task_') or a set of exact
names (e.g. {'schedule_task', 'list_crons', 'cancel_cron'}). A fallback handler
catches everything else (builtins, feishu_docs, mcp, search_channel_history).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)


@runtime_checkable
class ToolHandler(Protocol):
    """Protocol for all tool handlers.

    Each handler wraps an existing dispatch function and adapts it to the
    unified runtime interface.
    """

    schemas: list[dict]

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str: ...


class ToolDispatcher:
    """Unified tool dispatcher — replaces loop.py's 5 if/elif branches.

    Matching priority: exact name > prefix > fallback.

    Usage:
        dispatcher = get_dispatcher()
        result = await dispatcher.dispatch(rt, "task_create", {"title": "..."})
    """

    def __init__(self) -> None:
        self._prefix_handlers: dict[str, ToolHandler] = {}
        self._exact_handlers: dict[str, ToolHandler] = {}
        self._fallback: ToolHandler | None = None

    def register_prefix(self, prefix: str, handler: ToolHandler) -> None:
        """Register a handler for a tool name prefix (e.g. 'task_')."""
        self._prefix_handlers[prefix] = handler

    def register_exact(self, names: set[str], handler: ToolHandler) -> None:
        """Register a handler for exact tool names."""
        for name in names:
            self._exact_handlers[name] = handler

    def register_fallback(self, handler: ToolHandler) -> None:
        """Register the fallback handler (builtins, feishu_docs, mcp, etc.)."""
        self._fallback = handler

    def _find_handler(self, name: str) -> ToolHandler | None:
        """Find the matching handler. Priority: exact > prefix > fallback."""
        if name in self._exact_handlers:
            return self._exact_handlers[name]
        for prefix, handler in self._prefix_handlers.items():
            if name.startswith(prefix):
                return handler
        return self._fallback

    async def dispatch(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        """Find the matching handler and execute the tool."""
        handler = self._find_handler(name)
        if handler is None:
            logger.warning("Unknown tool: %s in channel=%s", name, rt.channel_id)
            return f"Tool '{name}' not found."
        return await handler.run(rt, name, args)

    def list_tools(self, channel_id: str, agent_id: str = "default") -> list[dict]:
        """Aggregate all tool schemas from registered handlers.

        Uses the existing registry to get channel-specific tools (builtins +
        feishu_docs + mcp), then appends task/reminder/cron schemas.
        """
        from ocl.agents.cron_store import CRON_TOOL_SCHEMAS
        from ocl.agents.reminder_store import REMINDER_TOOL_SCHEMAS
        from ocl.agents.task_store import TASK_TOOL_SCHEMAS
        from ocl.tools.registry import get_channel_tools
        from ocl.tools.sandbox.tools import SANDBOX_TOOL_SCHEMAS
        from ocl.tools.orchestration import ORCHESTRATION_TOOL_SCHEMAS
        from ocl.tools.capability import CAPABILITY_TOOLS

        tools = list(get_channel_tools(channel_id, agent_id))
        tools.extend(TASK_TOOL_SCHEMAS)
        tools.extend(REMINDER_TOOL_SCHEMAS)
        tools.extend(CRON_TOOL_SCHEMAS)
        tools.extend(SANDBOX_TOOL_SCHEMAS)
        tools.extend(ORCHESTRATION_TOOL_SCHEMAS)
        tools.extend(CAPABILITY_TOOLS)
        return tools


# ── Global dispatcher singleton ──────────────────────────────────────────────

_global_dispatcher: ToolDispatcher | None = None


def get_dispatcher() -> ToolDispatcher:
    """Get or create the global ToolDispatcher with all default handlers registered."""
    global _global_dispatcher
    if _global_dispatcher is None:
        _global_dispatcher = ToolDispatcher()
        _register_default_handlers(_global_dispatcher)
    return _global_dispatcher


def _register_default_handlers(dispatcher: ToolDispatcher) -> None:
    """Register all default tool handlers.

    Registration order matters for prefix matching, but exact names always
    take priority over prefixes. The fallback handler catches everything
    not matched by exact or prefix.
    """
    from ocl.runtime.handlers import (
        CronHandler,
        FallbackHandler,
        MemoryHandler,
        ReminderHandler,
        TaskHandler,
    )
    from ocl.tools.sandbox.tools import SandboxHandler
    from ocl.tools.orchestration import OrchestrationHandler
    from ocl.tools.capability import CapabilityHandler

    # Prefix handlers
    dispatcher.register_prefix("task_", TaskHandler())
    dispatcher.register_prefix("reminder_", ReminderHandler())

    # Exact name handlers
    dispatcher.register_exact(
        {"schedule_task", "list_crons", "cancel_cron"}, CronHandler()
    )
    dispatcher.register_exact(
        {"memory_append", "memory_replace", "memory_delete"}, MemoryHandler()
    )

    # Sandbox tools — exact names (exec_code, sandbox_*)
    dispatcher.register_exact(
        {
            "exec_code",
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_list_files",
            "sandbox_install_package",
        },
        SandboxHandler(),
    )

    # Orchestration tools — exact names (plan_subtasks, run_subtask, etc.)
    dispatcher.register_exact(
        {
            "plan_subtasks",
            "run_subtask",
            "wait_subtasks",
            "get_subtask_status",
            "retry_subtask",
        },
        OrchestrationHandler(),
    )

    # Capability discovery tools
    dispatcher.register_exact(
        {"list_capabilities", "describe_capability"},
        CapabilityHandler(),
    )

    # Fallback: builtins + feishu_docs + mcp + search_channel_history
    dispatcher.register_fallback(FallbackHandler())
