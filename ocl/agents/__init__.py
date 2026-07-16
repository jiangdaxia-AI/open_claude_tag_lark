"""Multi-agent support: registry, dispatcher, lifecycle, per-agent config, tasks, reminders, action cards."""

from ocl.agents.action_card import (
    ACTION_CARD_TOOL_SCHEMAS,
    action_prepare,
    action_wait,
    dispatch_action_card_tool,
    handle_card_callback,
)
from ocl.agents.config import (
    AgentConfig,
    ChannelAgentRegistry,
    clear_cache,
    load_agents,
    resolve_agent_from_text,
)
from ocl.agents.lifecycle import (
    AgentState,
    check_and_sleep_idle_agents,
    clear_lifecycles,
    get_lifecycle,
    set_callbacks,
    wake_agent,
)
from ocl.agents.reminder_store import (
    REMINDER_TOOL_SCHEMAS,
    dispatch_reminder_tool,
    init_reminder_scheduler,
    reminder_cancel,
    reminder_list,
    reminder_schedule,
)
from ocl.agents.task_store import (
    TASK_TOOL_SCHEMAS,
    dispatch_task_tool,
    task_assign,
    task_claim,
    task_create,
    task_get,
    task_list,
    task_update,
)

__all__ = [
    # Config
    "AgentConfig",
    "AgentState",
    "ChannelAgentRegistry",
    "clear_cache",
    "load_agents",
    "resolve_agent_from_text",
    # Lifecycle
    "check_and_sleep_idle_agents",
    "clear_lifecycles",
    "get_lifecycle",
    "set_callbacks",
    "wake_agent",
    # Tasks
    "TASK_TOOL_SCHEMAS",
    "dispatch_task_tool",
    "task_assign",
    "task_claim",
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    # Reminders
    "REMINDER_TOOL_SCHEMAS",
    "dispatch_reminder_tool",
    "init_reminder_scheduler",
    "reminder_cancel",
    "reminder_list",
    "reminder_schedule",
    # Action cards
    "ACTION_CARD_TOOL_SCHEMAS",
    "action_prepare",
    "action_wait",
    "dispatch_action_card_tool",
    "handle_card_callback",
]
