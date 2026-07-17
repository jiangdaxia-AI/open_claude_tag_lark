"""Sandbox lifecycle management — timeout cleanup and graceful shutdown.

Provides:
  - cleanup_stale_sandboxes: periodically destroy sandboxes that have been
    idle longer than the configured timeout.
  - graceful_shutdown: destroy all active sandboxes on process exit.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ocl.tools.sandbox.provider import get_provider

logger = logging.getLogger(__name__)

# Track last activity time per session
_last_activity: dict[str, float] = {}

# Background cleanup task handle
_cleanup_task: asyncio.Task | None = None

# Cleanup interval (seconds)
_CLEANUP_INTERVAL = 300  # 5 minutes


def touch_session(session_id: str) -> None:
    """Record that a session is still active."""
    _last_activity[session_id] = time.time()


async def cleanup_stale_sandboxes(max_idle_seconds: int = 1800) -> int:
    """Destroy sandboxes that have been idle longer than max_idle_seconds.

    Returns the number of sandboxes destroyed.
    """
    now = time.time()
    stale_sessions = [
        sid for sid, last_time in _last_activity.items()
        if now - last_time > max_idle_seconds
    ]

    provider = get_provider()
    for sid in stale_sessions:
        await provider.destroy(sid)
        _last_activity.pop(sid, None)

    if stale_sessions:
        logger.info("Cleaned up %d stale sandboxes", len(stale_sessions))

    return len(stale_sessions)


async def _cleanup_loop() -> None:
    """Background loop that periodically cleans up stale sandboxes."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            await cleanup_stale_sandboxes()
        except Exception:
            logger.exception("Sandbox cleanup loop error")


def start_cleanup_task() -> None:
    """Start the background cleanup task (idempotent)."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("Sandbox cleanup task started")


def stop_cleanup_task() -> None:
    """Stop the background cleanup task."""
    global _cleanup_task
    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
        _cleanup_task = None


async def graceful_shutdown() -> None:
    """Destroy all active sandboxes. Call on process exit."""
    stop_cleanup_task()
    provider = get_provider()
    await provider.destroy_all()
    _last_activity.clear()
    logger.info("All sandboxes destroyed on shutdown")
