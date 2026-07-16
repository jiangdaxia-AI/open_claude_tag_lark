"""Agent cancellation — users can interrupt a running agent loop.

Each agent run gets a cancellation token (asyncio.Event). When a user
sends "取消" / "cancel" / "stop" as a reply to the agent's message,
the token is set, and the agent loop checks it between rounds and
between tool calls, aborting gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# channel_id + agent_id → set of cancellation events (one per active run)
_cancel_tokens: dict[tuple[str, str], set[asyncio.Event]] = defaultdict(set)

_CANCEL_KEYWORDS = {"取消", "cancel", "stop", "中止", "停止", "中断"}


def create_cancel_token(channel_id: str, agent_id: str) -> asyncio.Event:
    """Create and register a cancellation token for an agent run."""
    token = asyncio.Event()
    _cancel_tokens[(channel_id, agent_id)].add(token)
    return token


def remove_cancel_token(channel_id: str, agent_id: str, token: asyncio.Event) -> None:
    """Remove a used token (call after the agent loop ends)."""
    _cancel_tokens[(channel_id, agent_id)].discard(token)


def cancel_agent(channel_id: str, agent_id: str | None = None) -> int:
    """Cancel all running agent loops for a channel (+ optional agent_id filter).
    Returns number of cancelled tokens."""
    count = 0
    for (ch, ag), tokens in list(_cancel_tokens.items()):
        if ch != channel_id:
            continue
        if agent_id and ag != agent_id:
            continue
        for token in tokens:
            if not token.is_set():
                token.set()
                count += 1
                logger.info("Cancelled agent %s in channel %s", ag, ch)
    return count


def is_cancelled(token: asyncio.Event) -> bool:
    return token.is_set()


def is_cancel_message(text: str) -> bool:
    """Check if a message is a cancellation command."""
    stripped = text.strip().lower().lstrip("@")
    return stripped in _CANCEL_KEYWORDS
