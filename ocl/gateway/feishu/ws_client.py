"""Feishu WebSocket client — entry point for Feishu events.

Wires up multi-agent support, reminder scheduler, action card callbacks,
lifecycle idle-sleep checking, and optional web admin dashboard.

On startup, auto-discovers bot_open_id for each agent that has app_id +
app_secret but no bot_open_id configured (users only need to provide
app_id + app_secret — the open_id is fetched automatically).

Architecture (preserves the proven lark-oapi pattern):
  main thread: cli.main() → ws_client.start()
    - import lark_oapi (captures its own event loop at module load)
    - ws.start()  ← blocking; runs the WebSocket loop forever
  background daemon thread: asyncio loop.run_forever()
    - runs gateway, agent loop, acompletion
    - HeartbeatService, APScheduler, idle lifecycle sleep check
"""

from __future__ import annotations

import asyncio
import logging
import threading

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ocl.agents.config import load_agents, get_all_initialized_channel_ids
from ocl.agents.reminder_store import init_reminder_scheduler
from ocl.agents.lifecycle import check_and_sleep_idle_agents, set_callbacks
from ocl.ambient.heartbeat import HeartbeatService
from ocl.config import settings
from ocl.gateway.feishu.auth import TokenManager
from ocl.gateway.feishu.events import FeishuEventHandler
from ocl.gateway.feishu.gateway import FeishuGateway
from ocl.gateway.router import get_session_lock
from ocl.llm import configure as configure_llm
from ocl.memory.store import get_store
from ocl.tools.mcp_client import MCPClientManager
from ocl.tools.mcp_config import load_all_channel_mcp_configs
from ocl.tools.registry import set_mcp_mgr, set_token_mgr

logger = logging.getLogger(__name__)

_cleaned_chats: set[str] = set()


async def _auto_discover_bot_open_ids() -> None:
    """For every agent that has app_id + app_secret but no bot_open_id,
    call the Feishu API to auto-discover the open_id and fill it in.

    This runs at startup so users only need to configure app_id + app_secret
    in agents.toml — the bot_open_id is fetched automatically.
    """
    from ocl.agents.config import _registry_cache

    discovered = 0
    for channel_id, registry in _registry_cache.items():
        for cfg in registry.iter_enabled():
            if cfg.feishu_bot_open_id:
                continue  # already configured
            if not cfg.feishu_app_id or not cfg.feishu_app_secret:
                continue  # no credentials to discover with
            # Create a temporary TokenManager for this agent's app credentials
            tm = TokenManager(cfg.feishu_app_id, cfg.feishu_app_secret)
            try:
                open_id = await tm.fetch_bot_open_id()
                if open_id:
                    cfg.feishu_bot_open_id = open_id
                    discovered += 1
                    logger.info(
                        "Auto-discovered bot_open_id for agent %s in channel %s: %s",
                        cfg.agent_id, channel_id, open_id,
                    )
            finally:
                await tm.close()
    if discovered:
        logger.info("Auto-discovered %d bot open_ids total", discovered)


async def ensure_bot_open_ids_discovered(channel_id: str) -> None:
    """Ensure bot_open_id is discovered for all agents in a channel.

    Called at runtime when a new channel receives its first message (the channel
    was auto-initialized from template at that point, so startup pre-warm missed it).
    Safe to call repeatedly — skips agents that already have a bot_open_id.
    """
    from ocl.agents.config import _registry_cache

    registry = _registry_cache.get(channel_id)
    if registry is None:
        return
    for cfg in registry.iter_enabled():
        if cfg.feishu_bot_open_id:
            continue
        if not cfg.feishu_app_id or not cfg.feishu_app_secret:
            continue
        tm = TokenManager(cfg.feishu_app_id, cfg.feishu_app_secret)
        try:
            open_id = await tm.fetch_bot_open_id()
            if open_id:
                cfg.feishu_bot_open_id = open_id
                logger.info(
                    "Runtime-discovered bot_open_id for agent %s in channel %s: %s",
                    cfg.agent_id, channel_id, open_id,
                )
        finally:
            await tm.close()


def _build_managers(token_mgr, gateway, bg_loop):
    """Construct MCPClientManager + HeartbeatService + scheduler.

    Wires up reminder scheduler and lifecycle callbacks.
    """
    # MCP
    mcp_mgr = MCPClientManager()
    mcp_mgr.set_channel_configs(load_all_channel_mcp_configs())
    set_mcp_mgr(mcp_mgr)

    # Heartbeat + scheduler
    scheduler = AsyncIOScheduler(event_loop=bg_loop)
    heartbeat = HeartbeatService(
        gateway=gateway,
        scheduler=scheduler,
        get_session_lock=get_session_lock,
    )
    heartbeat.start()
    scheduler.start()

    # Reminder scheduler
    init_reminder_scheduler(scheduler, gateway)

    # Lifecycle idle-sleep check (every 60s)
    async def _idle_check():
        sleeping = check_and_sleep_idle_agents()
        for channel_id, agent_id in sleeping:
            logger.info("Agent %s in %s went to sleep (idle)", agent_id, channel_id)

    scheduler.add_job(_idle_check, "interval", seconds=60, id="idle-sleep-check")

    # Lifecycle callbacks
    set_callbacks(
        on_sleep=lambda ch, ag: logger.info("Agent %s in channel %s went to sleep", ag, ch),
        on_wake=lambda ch, ag: logger.info("Agent %s in channel %s woke up", ag, ch),
    )

    return mcp_mgr, heartbeat, scheduler


def start() -> None:
    """Program entry: wire everything together and start the WebSocket loop.

    Synchronous and blocking — call from cli.main() directly, NOT inside
    asyncio.run().
    """
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise SystemExit(
            "Missing FEISHU_APP_ID or FEISHU_APP_SECRET. "
            "Get them from https://open.feishu.cn/app"
        )
    if not settings.feishu_tenant_id:
        raise SystemExit(
            "Missing FEISHU_TENANT_ID. Required for session key isolation."
        )

    configure_llm()

    token_mgr = TokenManager(settings.feishu_app_id, settings.feishu_app_secret)
    gateway = FeishuGateway(token_mgr=token_mgr, tenant_id=settings.feishu_tenant_id)

    # Start background loop early — auto-discovery needs it
    bg_loop = asyncio.new_event_loop()
    bg_thread = threading.Thread(
        target=_run_bg_loop, args=(bg_loop,), name="ocl-async", daemon=True
    )
    bg_thread.start()

    # ── Auto-discover primary bot open_id ──────────────────────────
    # If FEISHU_BOT_OPEN_ID not set in .env, fetch it automatically.
    if not settings.feishu_bot_open_id:
        logger.info("FEISHU_BOT_OPEN_ID not set — auto-discovering via Feishu API...")
        try:
            fut = asyncio.run_coroutine_threadsafe(
                token_mgr.fetch_bot_open_id(), bg_loop
            )
            open_id = fut.result(timeout=15)
            if open_id:
                settings.feishu_bot_open_id = open_id
                logger.info("Primary bot open_id auto-discovered: %s", open_id)
            else:
                logger.warning(
                    "Could not auto-discover primary bot open_id — "
                    "group @-filtering disabled"
                )
        except Exception as exc:
            logger.warning("Failed to auto-discover primary bot open_id: %s", exc)

    # ── Pre-warm agent registries from global template ────────
    # No need to scan per-channel dirs manually — template is auto-applied
    # when the first message arrives in each new channel.
    # However, we DO pre-warm any channels that already exist on disk
    # so their bot_open_ids are available for auto-discovery at startup.
    for channel_id in get_all_initialized_channel_ids():
        load_agents(channel_id)

    # ── Auto-discover bot_open_id for all multi-agent bots ────────
    # Each agent with app_id + app_secret but no bot_open_id gets
    # its open_id fetched automatically via the Feishu bot info API.
    asyncio.run_coroutine_threadsafe(
        _auto_discover_bot_open_ids(), bg_loop
    ).result(timeout=30)

    event_handler = FeishuEventHandler(
        gateway=gateway,
        bot_open_id=settings.feishu_bot_open_id or None,
        tenant_id=settings.feishu_tenant_id,
    )

    async def store_getter(chat_id: str) -> "object | None":
        if chat_id not in _cleaned_chats:
            store = await get_store(settings.feishu_tenant_id, chat_id)
            if store:
                try:
                    deleted = await store.cleanup_old_events(days=7)
                    if deleted > 0:
                        logger.info("Cleaned %d old events for chat %s", deleted, chat_id)
                except Exception as e:
                    logger.warning("Failed to cleanup old events for chat %s: %s", chat_id, e)
                _cleaned_chats.add(chat_id)
            return store
        return await get_store(settings.feishu_tenant_id, chat_id)

    event_handler._store_getter = store_getter
    set_token_mgr(token_mgr)

    mcp_mgr, _heartbeat, _scheduler = _build_managers(token_mgr, gateway, bg_loop)

    # Start web admin server if enabled
    if settings.web_admin_enabled:
        try:
            from ocl.web_admin import start_admin_server
            start_admin_server(
                host=settings.web_admin_host,
                port=settings.web_admin_port,
                bg_loop=bg_loop,
            )
            logger.info("Web admin started on %s:%d", settings.web_admin_host, settings.web_admin_port)
        except ImportError:
            logger.warning("Web admin disabled — fastapi/uvicorn not installed. pip install fastapi uvicorn")
        except Exception as exc:
            logger.warning("Failed to start web admin: %s", exc)

    logger.info(
        "Feishu gateway starting (tenant=%s, bot_open_id=%s, idle_timeout=%ds)",
        settings.feishu_tenant_id,
        (settings.feishu_bot_open_id or "<disabled>"),
        settings.agent_idle_timeout_seconds,
    )

    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    import lark_oapi as lark
    from lark_oapi.ws.client import Client as WsClient

    def on_message(event: P2ImMessageReceiveV1) -> None:
        """lark-oapi callback; runs on its loop in the main thread."""
        try:
            event_dict = _event_to_dict(event)
        except Exception:
            logger.exception("Failed to serialize Feishu event")
            return
        fut = asyncio.run_coroutine_threadsafe(
            event_handler.on_message_receive(event_dict), bg_loop
        )
        fut.add_done_callback(_log_bg_failure)

    def on_card_action(event) -> None:
        """Handle Feishu interactive card button clicks (action card callbacks)."""
        try:
            event_dict = _event_to_dict(event)
            fut = asyncio.run_coroutine_threadsafe(
                _handle_card_action(event_dict), bg_loop
            )
            fut.add_done_callback(_log_bg_failure)
        except Exception:
            logger.exception("Failed to handle card action event")

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_message_read_v1(_noop_event)
        .build()
    )

    ws = WsClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

    # Watchdog: lark-oapi _select() blocks forever and doesn't detect dead
    # connections. Check ws._conn every 20s; if dead, exit process for restart.
    import os as _os
    import threading as _threading
    import time as _wt

    def _ws_watchdog():
        _wt.sleep(15)
        while True:
            conn = getattr(ws, "_conn", None)
            if conn is None:
                logger.error("Watchdog: ws._conn is None — exiting for restart")
                _os._exit(1)
            # websockets 15: check close_code (set when connection closes)
            close_code = getattr(conn, "close_code", None)
            if close_code is not None:
                logger.error("Watchdog: ws connection closed (code=%s) — exiting for restart", close_code)
                _os._exit(1)
            _wt.sleep(20)

    _threading.Thread(target=_ws_watchdog, name="ws-watchdog", daemon=True).start()

    # Start sandbox cleanup task (destroys idle sandboxes periodically)
    # start_cleanup_task calls asyncio.create_task which needs a running loop,
    # so schedule it on the bg_loop instead of the main thread.
    if settings.sandbox_enabled:
        from ocl.tools.sandbox.lifecycle import start_cleanup_task
        bg_loop.call_soon_threadsafe(start_cleanup_task)
        logger.info("Sandbox cleanup task started")

    try:
        ws.start()  # blocking
    finally:
        try:
            await_coro = asyncio.run_coroutine_threadsafe(mcp_mgr.shutdown(), bg_loop)
            await_coro.result(timeout=5)
        except Exception:
            logger.exception("MCP shutdown failed")
        try:
            _heartbeat.shutdown()
        except Exception:
            logger.exception("Heartbeat shutdown failed")
        # Destroy all active sandboxes on shutdown
        if settings.sandbox_enabled:
            try:
                from ocl.tools.sandbox.lifecycle import graceful_shutdown
                await_coro = asyncio.run_coroutine_threadsafe(graceful_shutdown(), bg_loop)
                await_coro.result(timeout=10)
            except Exception:
                logger.exception("Sandbox graceful shutdown failed")
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        bg_thread.join(timeout=5)


async def _handle_card_action(event_dict: dict) -> None:
    """Handle a card.action.trigger event — resolve pending action cards."""
    from ocl.agents.action_card import handle_card_callback

    try:
        action_value = (
            event_dict.get("event", {})
            .get("action", {})
            .get("value", {})
        )
        if isinstance(action_value, str):
            import json
            action_value = json.loads(action_value)
        if action_value:
            result = await handle_card_callback(action_value)
            logger.info("Card action resolved: %s", result)
    except Exception:
        logger.exception("Failed to handle card action")


def _run_bg_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _noop_event(_event: object) -> None:
    return None


def _log_bg_failure(fut: "asyncio.Future[None]") -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        logger.error("Background handler raised: %r", exc, exc_info=exc)


def _event_to_dict(obj: object) -> object:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_event_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _event_to_dict(v) for k, v in obj.items()}
    out: dict = {}
    for k, v in vars(obj).items():
        if k.startswith("_"):
            continue
        if v is None:
            continue
        out[k] = _event_to_dict(v)
    return out
