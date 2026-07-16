"""Agent lifecycle: active / sleeping state machine with idle timeout.

Each agent in each channel has a lifecycle state:
  - active: agent is awake, responding to messages, has warm context
  - sleeping: agent has been idle for > idle_timeout_seconds; context is cold

When sleeping and a new @mention arrives, the agent wakes up:
  1. Re-read AGENT.md and MEMORY.md
  2. Load recent channel messages as context
  3. Resume normal operation

The heartbeat scheduler checks periodically for sleeping-eligible agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class AgentState(Enum):
    ACTIVE = "active"
    SLEEPING = "sleeping"


@dataclass
class AgentLifecycle:
    """Tracks the lifecycle state of one agent in one channel."""

    agent_id: str
    channel_id: str
    state: AgentState = AgentState.SLEEPING
    last_activity_ts: float = field(default_factory=time.time)
    idle_timeout_seconds: int = 600  # 10 min default

    # Per-agent asyncio.Event to signal wake
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self) -> None:
        """Record activity (message received or sent)."""
        self.last_activity_ts = time.time()
        self.state = AgentState.ACTIVE
        self._wake_event.set()

    def sleep(self) -> None:
        """Put this agent to sleep (context will be cold on next wake)."""
        self.state = AgentState.SLEEPING
        self._wake_event.clear()
        logger.info(
            "Agent %s in channel %s went to sleep", self.agent_id, self.channel_id
        )

    def should_sleep(self) -> bool:
        """Check if this agent has been idle long enough to sleep."""
        if self.state != AgentState.ACTIVE:
            return False
        idle_seconds = time.time() - self.last_activity_ts
        return idle_seconds >= self.idle_timeout_seconds

    async def wait_for_wake(self, timeout: float | None = None) -> bool:
        """Block until the agent is woken. Returns True if woken, False on timeout."""
        if self.state == AgentState.ACTIVE:
            return True
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_active(self) -> bool:
        return self.state == AgentState.ACTIVE


# ── global registry: (channel_id, agent_id) → AgentLifecycle ──

_lifecycles: dict[tuple[str, str], AgentLifecycle] = {}

# Optional callbacks (set externally)
_on_sleep: Callable[[str, str], None] | None = None  # (channel_id, agent_id)
_on_wake: Callable[[str, str], None] | None = None


def get_lifecycle(
    channel_id: str, agent_id: str, idle_timeout: int = 600
) -> AgentLifecycle:
    """Get or create the lifecycle tracker for this (channel, agent) pair."""
    key = (channel_id, agent_id)
    if key not in _lifecycles:
        _lifecycles[key] = AgentLifecycle(
            agent_id=agent_id,
            channel_id=channel_id,
            idle_timeout_seconds=idle_timeout,
        )
    return _lifecycles[key]


def wake_agent(channel_id: str, agent_id: str) -> AgentLifecycle:
    """Wake up an agent (if sleeping) and mark it as active."""
    lc = get_lifecycle(channel_id, agent_id)
    if not lc.is_active:
        logger.info("Waking agent %s in channel %s", agent_id, channel_id)
        if _on_wake:
            _on_wake(channel_id, agent_id)
    lc.touch()
    return lc


def check_and_sleep_idle_agents() -> list[tuple[str, str]]:
    """Check all active agents for idle timeout.

    Returns list of (channel_id, agent_id) tuples that went to sleep.
    Called periodically by the heartbeat scheduler.
    """
    sleeping: list[tuple[str, str]] = []
    for (channel_id, agent_id), lc in list(_lifecycles.items()):
        if lc.should_sleep():
            lc.sleep()
            sleeping.append((channel_id, agent_id))
            if _on_sleep:
                _on_sleep(channel_id, agent_id)
    return sleeping


def set_callbacks(
    on_sleep: Callable[[str, str], None] | None = None,
    on_wake: Callable[[str, str], None] | None = None,
) -> None:
    """Set optional lifecycle callbacks."""
    global _on_sleep, _on_wake
    _on_sleep = on_sleep
    _on_wake = on_wake


def clear_lifecycles() -> None:
    """Clear all lifecycle tracking (for tests)."""
    _lifecycles.clear()
