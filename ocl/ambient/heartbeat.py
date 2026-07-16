"""Ambient heartbeat — proactive channel monitoring + idle-sleep checking.

HeartbeatService holds an APScheduler and registers one cron job per channel
that has an enabled HEARTBEAT.md. When a job fires it runs run_once, which:
reads recent messages from the channel store, asks the LLM whether anything
is worth surfacing, and posts via the Gateway unless the LLM said SILENT.

Also checks for idle agents that should be put to sleep.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ocl.ambient.config import HeartbeatConfig, load_channel_heartbeat_config
from ocl.agents.lifecycle import check_and_sleep_idle_agents
from ocl.config import settings
from ocl.llm import acompletion
from ocl.memory.store import get_store

if TYPE_CHECKING:
    import asyncio
    from ocl.gateway.base import Gateway

logger = logging.getLogger(__name__)

_HEARTBEAT_PROMPT = """\
You are monitoring a channel as a proactive AI teammate.
Below is a summary of recent activity.

Your job: decide if anything is worth surfacing proactively.
Only post if there's genuine value — a stale thread needing follow-up,
a deadline approaching, an unresolved question, or a risk you spotted.

If nothing is worth surfacing, respond with exactly: SILENT
Otherwise, write the message you would post to the channel (concise, actionable).
"""


class HeartbeatService:
    def __init__(
        self,
        gateway: "Gateway",
        scheduler: AsyncIOScheduler,
        get_session_lock: Callable[..., "asyncio.Lock"],
    ) -> None:
        self._gateway = gateway
        self._scheduler = scheduler
        self._get_session_lock = get_session_lock

    def start(self) -> None:
        """Register heartbeat jobs for all channels with HEARTBEAT.md configs."""
        if not settings.channels_dir.exists():
            return
        for channel_dir in sorted(settings.channels_dir.iterdir()):
            if not channel_dir.is_dir():
                continue
            hb_config = load_channel_heartbeat_config(channel_dir.name)
            if hb_config and hb_config.enabled:
                self._register_channel(hb_config)
                logger.info("Heartbeat registered for channel=%s", channel_dir.name)

    def _register_channel(self, config: HeartbeatConfig) -> None:
        trigger = CronTrigger.from_crontab(config.cron)
        self._scheduler.add_job(
            self._run_heartbeat,
            trigger=trigger,
            args=[config.channel_id, config.guidance or "", config.max_recent],
            id=f"heartbeat-{config.channel_id}",
            replace_existing=True,
        )

    async def _run_heartbeat(
        self, channel_id: str, guidance: str, max_recent: int
    ) -> None:
        """Single heartbeat evaluation + idle-sleep check."""
        # Check for idle agents to sleep
        sleeping = check_and_sleep_idle_agents()
        for ch_id, ag_id in sleeping:
            logger.info("Agent %s in %s went to sleep (idle)", ag_id, ch_id)

        lock = self._get_session_lock(self._gateway.tenant_id, channel_id, "default")
        async with lock:
            store = await get_store(self._gateway.tenant_id, channel_id)
            recent = await store.get_recent_messages(limit=max_recent)

            if not recent:
                return

            summary = self._format_recent(recent)
            prompt = f"{_HEARTBEAT_PROMPT}\n\nRecent activity:\n{summary}"
            if guidance:
                prompt += f"\n\nAdditional guidance: {guidance}"

            try:
                response = await acompletion(
                    channel_id=channel_id,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.choices[0].message.content or ""
                if text.strip().upper() == "SILENT":
                    return
                await self._gateway.send_message(chat_id=channel_id, text=text.strip())
            except Exception:
                logger.exception("Heartbeat failed for channel=%s", channel_id)

    def _format_recent(self, rows: list) -> str:
        lines = []
        for r in rows:
            ts = r["ts"][:16].replace("T", " ")
            lines.append(f"[{ts} @{r['display_name']}] {r['content']}")
        return "\n".join(lines)

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
