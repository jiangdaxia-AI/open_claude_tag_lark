"""FeishuEventHandler — dispatches im.message.receive_v1 events for multi-agent mode.

Multi-bot flow (Q1:B):
  User sends "@CodeBot review this" in group chat
  → Feishu delivers event with mentions=[{open_id: bot_codebot}]
  → handler looks up bot_open_id → finds AgentConfig for CodeBot
  → routes to route_message(agent_id="codebot")

Backward compat: if no agents.toml exists, falls back to single-bot mode.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ocl.agents.config import load_agents
from ocl.gateway.feishu.text import clean_at_tags
from ocl.gateway.router import route_message

if TYPE_CHECKING:
    from ocl.gateway.base import Gateway

logger = logging.getLogger(__name__)


class FeishuEventHandler:
    def __init__(
        self,
        gateway: "Gateway",
        bot_open_id: str | None,
        tenant_id: str,
    ) -> None:
        self._gateway = gateway
        self._bot_open_id = bot_open_id
        self._tenant_id = tenant_id
        self._bot_open_id_warned = False

    async def on_message_receive(self, event: dict) -> None:
        try:
            await self._handle(event)
        except Exception:
            event_id = event.get("header", {}).get("event_id", "<unknown>")
            logger.exception("Failed to handle event %s", event_id)

    async def _handle(self, event: dict) -> None:
        event_id = event["header"]["event_id"]
        event_body = event["event"]
        message = event_body["message"]

        chat_type = message.get("chat_type", "")
        sender_id = event_body["sender"]["sender_id"]["open_id"]
        message_id = message["message_id"]
        chat_id = message.get("chat_id")

        mentions = message.get("mentions", []) or []

        if not chat_id:
            logger.warning("No chat_id in event %s", event_id)
            return

        # 1. Load channel agents and collect ALL bot_open_ids for loop prevention
        try:
            registry = load_agents(chat_id)
            # Runtime discovery: new channels auto-initialized from template at
            # first message missed the startup pre-warm. Discover bot_open_ids now.
            await _ensure_bot_open_ids(registry, chat_id, self._gateway)
        except Exception:
            logger.exception("Failed to load agents or discover bot_open_ids for chat=%s", chat_id)
            registry = None

        all_bot_ids: set[str] = set()
        if registry:
            for cfg in registry.iter_enabled():
                if cfg.feishu_bot_open_id:
                    all_bot_ids.add(cfg.feishu_bot_open_id)
        if self._bot_open_id:
            all_bot_ids.add(self._bot_open_id)

        if sender_id in all_bot_ids:
            logger.debug("Ignoring own bot message msg_id=%s", message_id)
            return

        # 2. Multi-bot mention routing
        is_group = chat_type == "group"
        mentions = message.get("mentions", []) or []
        agent_id = "default"

        if is_group:
            # Collect mentioned bots (open_id + name)
            mentioned_bots = [
                {"open_id": m.get("id", {}).get("open_id", ""), "name": m.get("name", "")}
                for m in mentions
                if m.get("mentioned_type") == "bot"
            ]

            # First pass: match by open_id (exact)
            matched_agent = None
            for mb in mentioned_bots:
                if mb["open_id"] in all_bot_ids and registry:
                    matched_agent = registry.get_by_bot_open_id(mb["open_id"])
                    if matched_agent:
                        logger.info("Routed to agent=%s via bot_open_id=%s", matched_agent.agent_id, mb["open_id"])
                        break

            # Second pass: match by mention name → agent display_name
            # (handles cases where the bot's open_id in the event differs from
            #  what fetch_bot_open_id returns)
            if not matched_agent and registry:
                for mb in mentioned_bots:
                    if not mb["name"]:
                        continue
                    matched_agent = registry.get_by_display_name(mb["name"])
                    if matched_agent:
                        logger.info("Routed to agent=%s via name=%s", matched_agent.agent_id, mb["name"])
                        break

            if not matched_agent and not mentioned_bots:
                logger.debug("Group msg %s without @any_bot — skipping", message_id)
                return

            if matched_agent:
                agent_id = matched_agent.agent_id

        elif chat_type not in ("group", "p2p"):
            logger.debug("Unknown chat_type '%s' for msg_id=%s", chat_type, message_id)

        # 3. Idempotency
        if await self._is_processed(event_id, chat_id):
            logger.debug("Dropping duplicate event %s", event_id)
            return

        raw_text = self._extract_text(message, mentions=mentions)
        cleaned_text = clean_at_tags(raw_text)

        # Check for cancellation command
        from ocl.agents.cancel import is_cancel_message, cancel_agent
        if is_cancel_message(cleaned_text):
            count = cancel_agent(chat_id, agent_id if agent_id != "default" else None)
            await self._gateway.send_message(
                chat_id=chat_id,
                text=f"已取消 {count} 个正在运行的 agent 任务。" if count > 0 else "没有正在运行的 agent 任务。",
                reply_to=message_id,
            )
            await self._mark_processed(event_id, "im.message.receive_v1", chat_id)
            return

        # 4. Route
        logger.debug("Routing event %s chat=%s sender=%s agent=%s", event_id, chat_id, sender_id, agent_id)
        await route_message(
            gateway=self._gateway,
            tenant_id=self._tenant_id,
            chat_id=chat_id,
            user_id=sender_id,
            text=cleaned_text,
            message_id=message_id,
            agent_id=agent_id,
        )

        # 5. Mark processed
        await self._mark_processed(event_id, "im.message.receive_v1", chat_id)

    def _extract_text(self, message: dict, mentions: list | None = None) -> str:
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            return ""
        text = content.get("text", "") or content.get("content", "") or ""

        # Replace @_user_N placeholders with real mention names
        # Feishu encodes @mentions in text as @_user_1, @_user_2, etc.
        # The mentions array maps key → {name, id}.
        # Sort by key length desc to avoid @_user_1 matching @_user_10's prefix.
        if mentions:
            sorted_mentions = sorted(mentions, key=lambda m: len(m.get("key", "")), reverse=True)
            for m in sorted_mentions:
                key = m.get("key", "")
                name = m.get("name", "")
                if key and name and key in text:
                    text = text.replace(key, f"@{name}")
        return text

    _store_getter = None

    async def _is_processed(self, event_id: str, chat_id: str) -> bool:
        store = await self._get_store(chat_id)
        if store is None:
            return False
        return await store.is_event_processed(event_id)

    async def _mark_processed(self, event_id: str, event_type: str, chat_id: str) -> None:
        store = await self._get_store(chat_id)
        if store is None:
            return
        await store.mark_event_processed(event_id, event_type)

    async def _get_store(self, chat_id: str):
        if self._store_getter is None:
            return None
        if not chat_id:
            return None
        return await self._store_getter(chat_id)


async def _ensure_bot_open_ids(registry, chat_id: str, gateway=None) -> None:
    """Discover bot_open_id for any agent missing it.

    Also registers a per-agent TokenManager on the gateway so that agent's
    messages are sent as the correct bot (multi-bot mode).

    Inlined here (instead of imported from ws_client) to avoid circular imports:
    ws_client imports FeishuEventHandler from this module at module load time.
    """
    from ocl.gateway.feishu.auth import TokenManager


    for cfg in registry.iter_enabled():
        # Register a persistent TokenManager for this agent on the gateway
        # (needed for multi-bot mode: each agent sends messages as its own bot)
        if gateway is not None and cfg.feishu_app_id and cfg.feishu_app_secret:
            if not hasattr(gateway, "_agent_token_mgrs") or cfg.agent_id not in gateway._agent_token_mgrs:
                agent_tm = TokenManager(cfg.feishu_app_id, cfg.feishu_app_secret)
                if hasattr(gateway, "register_agent_token_manager"):
                    gateway.register_agent_token_manager(cfg.agent_id, agent_tm)

        if cfg.feishu_bot_open_id:
            continue
        if not cfg.feishu_app_id or not cfg.feishu_app_secret:
            continue
        tm = TokenManager(cfg.feishu_app_id, cfg.feishu_app_secret)
        try:
            open_id = await tm.fetch_bot_open_id()
            if open_id:
                cfg.feishu_bot_open_id = open_id
        except Exception:
            pass
        finally:
            await tm.close()
