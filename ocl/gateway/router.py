"""Channel router — maps (workspace_id, channel_id, agent_id) to an AgentSession and runs the loop.

Multi-agent routing:
  - If agent_id is explicitly provided (from multi-bot @mention), use it directly.
  - Otherwise, parse the message text for @Agent mentions to resolve the target.
  - If no @Agent is found, fall back to the default agent (Q2:A).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ocl.agents.config import resolve_agent_from_text
from ocl.memory.store import get_store

if TYPE_CHECKING:
    from ocl.gateway.base import Gateway

logger = logging.getLogger(__name__)

_sessions: dict[tuple[str, str, str], "AgentSession"] = {}


@dataclass
class AgentSession:
    workspace_id: str
    channel_id: str
    agent_id: str = "default"
    _lock: asyncio.Lock = field(default_factory=lambda: asyncio.Lock())


def get_or_create_session(
    workspace_id: str, channel_id: str, agent_id: str = "default"
) -> AgentSession:
    key = (workspace_id, channel_id, agent_id)
    if key not in _sessions:
        _sessions[key] = AgentSession(
            workspace_id=workspace_id,
            channel_id=channel_id,
            agent_id=agent_id,
        )
        logger.info("New session: workspace=%s channel=%s agent=%s", workspace_id, channel_id, agent_id)
    return _sessions[key]


def get_session_lock(
    workspace_id: str, channel_id: str, agent_id: str = "default"
) -> asyncio.Lock:
    return get_or_create_session(workspace_id, channel_id, agent_id)._lock


async def route_message(
    gateway: "Gateway",
    tenant_id: str,
    chat_id: str,
    user_id: str,
    text: str,
    message_id: str,
    agent_id: str = "default",
    _delegation_depth: int = 0,
    _upstream_agents: set[str] | None = None,
) -> None:
    """Route a message to the correct agent, handling @mention resolution.

    1. If agent_id is explicitly provided (multi-bot @mention), use it directly.
    2. Otherwise, parse text for @Agent mentions to resolve the target.
    3. If no agent is found, fall back to the default agent.
    """
    # Resolve agent_id from text mentions if not explicitly provided
    if agent_id == "default":
        try:
            resolved_id, cleaned_text = resolve_agent_from_text(chat_id, text)
            if resolved_id != "default":
                agent_id = resolved_id
                text = cleaned_text
                logger.info("Resolved agent_id=%s from text mention", agent_id)
        except Exception:
            logger.debug("Agent text resolution failed, using default")

    session = get_or_create_session(tenant_id, chat_id, agent_id)
    store = await get_store(tenant_id, chat_id)

    try:
        display_name = await gateway.get_user_name(user_id)
    except Exception:
        display_name = user_id

    async with session._lock:
        try:
            # Lazy import to avoid pulling litellm at module load time
            from ocl.agent.loop import run_agent_loop

            await run_agent_loop(
                gateway=gateway,
                workspace_id=tenant_id,
                channel_id=chat_id,
                user_id=user_id,
                display_name=display_name,
                text=text,
                message_id=message_id,
                store=store,
                agent_id=agent_id,
                _delegation_depth=_delegation_depth,
                _upstream_agents=_upstream_agents,
            )
        except Exception:
            logger.exception("Error handling message in chat %s for agent %s", chat_id, agent_id)
            try:
                await gateway.send_message(
                    chat_id=chat_id,
                    text="⚠️ 处理消息时出错，请稍后重试。",
                    reply_to=message_id,
                )
            except Exception:
                pass
