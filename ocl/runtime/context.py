"""AgentRuntime — unified runtime context for agent loop and all tools.

Replaces the 12+ parameter pass-through pattern in loop.py with a single
dataclass. Tools access their dependencies through this runtime, not through
individual function parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ocl.agents.ledger import LedgerEntry
    from ocl.gateway.base import Gateway
    from ocl.memory.store import MessageStore
    from ocl.runtime.dispatcher import ToolDispatcher


@dataclass
class AgentRuntime:
    """Runtime context — the single interface shared by agent loop and all tools.

    Fields are grouped by concern:
      - Session identity: who, where, which conversation
      - Capability injection: gateway, store, ledger
      - Delegation context: depth, upstream chain
      - Tool execution: dispatcher reference
    """

    # Session identity
    channel_id: str
    agent_id: str
    workspace_id: str
    session_id: str
    user_id: str
    display_name: str

    # Capability injection
    gateway: "Gateway"
    store: "MessageStore"
    ledger_entry: "LedgerEntry"
    cancel_token: str

    # Delegation context
    delegation_depth: int = 0
    upstream_agents: set[str] = field(default_factory=set)
    delegation_task_id: int | None = None

    # Tool execution
    dispatcher: "ToolDispatcher | None" = None

    async def exec_tool(self, name: str, args: dict) -> str:
        """Unified tool execution entry — all tools dispatched through here.

        Replaces the 5-branch if/elif in loop.py with a single dispatch call.
        """
        if self.dispatcher is None:
            return f"Tool '{name}' not available — dispatcher not configured."
        return await self.dispatcher.dispatch(self, name, args)

    def list_tools(self) -> list[dict]:
        """List all available tool schemas for this channel.

        Aggregates builtins + feishu_docs + mcp + task + reminder + cron tools.
        """
        if self.dispatcher is None:
            return []
        return self.dispatcher.list_tools(self.channel_id)
