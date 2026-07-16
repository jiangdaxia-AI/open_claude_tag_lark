"""Action Card system — human-in-the-loop approval for dangerous operations.

When an agent needs to perform a high-risk action (deploy, create group, invite
members, etc.), it sends an interactive Feishu card with confirm/cancel buttons.
The human clicks to approve, and the agent proceeds.

Flow:
  1. Agent calls action_prepare(action, details) → sends Feishu interactive card
  2. User clicks [✅ 确认] or [❌ 取消] → Feishu card.action.trigger callback fires
  3. handle_card_callback resolves the pending action via asyncio.Event
  4. Agent waits via action_wait(card_id) → returns "approved" or "rejected"

Storage: SQLite (pending_actions table) for persistence + asyncio.Event for async waiting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import aiosqlite

from ocl.config import settings

logger = logging.getLogger(__name__)

_CREATE_ACTIONS = """
CREATE TABLE IF NOT EXISTS pending_actions (
    card_id     TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
    resolved_at REAL
);
"""

# In-memory event store for async waiting
_pending_events: dict[str, asyncio.Event] = {}
_pending_results: dict[str, str] = {}


async def _get_db() -> aiosqlite.Connection:
    db_path = settings.data_dir / "workspaces" / "actions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_CREATE_ACTIONS)
    await db.commit()
    return db


def build_action_card(
    card_id: str,
    action: str,
    details: str,
    agent_name: str,
) -> dict[str, Any]:
    """Build a Feishu interactive card JSON for confirm/cancel."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⚠️ {agent_name} 需要你的确认"},
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{agent_name}** 即将执行以下操作：\n\n**{action}**\n\n{details}"},
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 确认"},
                        "type": "primary",
                        "value": {"card_id": card_id, "result": "approved"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "❌ 取消"},
                        "type": "danger",
                        "value": {"card_id": card_id, "result": "rejected"},
                    },
                ],
            },
        ],
    }


async def action_prepare(
    channel_id: str,
    agent_id: str,
    agent_name: str,
    action: str,
    details: str,
    gateway=None,
) -> dict[str, Any]:
    """Prepare an action card, send it to the channel, and return card_id + status.

    The caller should then call action_wait(card_id) to block until human responds.
    """
    card_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    _pending_events[card_id] = event
    _pending_results[card_id] = "pending"

    # Persist to SQLite
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO pending_actions (card_id, action, details, channel_id, agent_id)
               VALUES (?, ?, ?, ?, ?)""",
            (card_id, action, details, channel_id, agent_id),
        )
        await db.commit()
    finally:
        await db.close()

    # Build and send interactive card
    card = build_action_card(card_id, action, details, agent_name)

    if gateway is not None:
        try:
            token = gateway._token_mgr.get_tenant_token()
            payload = {
                "receive_id": channel_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            }
            resp = await gateway._client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            logger.info("Action card sent: card_id=%s channel=%s", card_id, channel_id)
        except Exception as exc:
            logger.warning("Failed to send action card: %s", exc)
            # Fallback: send as plain text
            if gateway is not None:
                try:
                    await gateway.send_message(
                        chat_id=channel_id,
                        text=f"⚠️ {agent_name} 需要确认: {action}\n{details}\n请回复'确认'或'取消'",
                    )
                except Exception:
                    pass

    logger.info("Action card created: card_id=%s action=%s channel=%s", card_id, action, channel_id)
    return {"card_id": card_id, "card": card}


async def action_wait(card_id: str, timeout: float = 300) -> str:
    """Wait for a human to approve/reject an action card.

    Returns 'approved', 'rejected', or 'timeout'.
    """
    event = _pending_events.get(card_id)
    if event is None:
        return "rejected"

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _pending_results[card_id] = "timeout"
        _cleanup(card_id)
        return "timeout"

    result = _pending_results.get(card_id, "rejected")
    _cleanup(card_id)
    return result


def _cleanup(card_id: str) -> None:
    _pending_events.pop(card_id, None)
    _pending_results.pop(card_id, None)


async def handle_card_callback(value: dict[str, Any]) -> str | None:
    """Handle a Feishu card button click callback.

    Called from ws_client when a card.action.trigger event arrives.
    `value` is the button's value dict: {card_id, result}

    Returns the result string ("approved" / "rejected") or None if unrecognized.
    """
    card_id = value.get("card_id")
    result = value.get("result", "rejected")

    if not card_id:
        logger.warning("Card callback missing card_id: %s", value)
        return None

    # Update SQLite
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = unixepoch('now','subsec') WHERE card_id = ?",
            (result, card_id),
        )
        await db.commit()
    finally:
        await db.close()

    # Resolve the async waiter
    event = _pending_events.get(card_id)
    if event:
        _pending_results[card_id] = result
        event.set()
        logger.info("Action card resolved: card_id=%s result=%s", card_id, result)
    else:
        logger.warning("Card callback for unknown/expired card_id=%s", card_id)

    return result


def cleanup_expired_actions(max_age_seconds: int = 600) -> int:
    """Remove expired pending actions from memory. Returns count cleaned up."""
    now = time.time()
    expired = [
        cid for cid, data in list(_pending_events.items())
        if now - data.get("created_at", now) > max_age_seconds
    ]
    for cid in expired:
        _pending_results[cid] = "timeout"
        _pending_events[cid].set()
    return len(expired)


# ── LiteLLM tool schemas ──

ACTION_CARD_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "action_prepare",
            "description": (
                "Send an interactive approval card to the channel. "
                "Use before performing dangerous or irreversible operations "
                "(deploys, deletions, member invitations). "
                "The card has Confirm/Cancel buttons. "
                "You will receive the result before proceeding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Short description of the action (e.g. 'Deploy auth module to production')",
                    },
                    "details": {
                        "type": "string",
                        "description": "Detailed explanation of what will happen and why",
                    },
                },
                "required": ["action", "details"],
            },
        },
    },
]


async def dispatch_action_card_tool(
    fn_name: str,
    args: dict[str, Any],
    channel_id: str,
    agent_id: str,
    agent_name: str,
    gateway=None,
) -> Any:
    """Dispatch action card tool calls."""
    if fn_name == "action_prepare":
        result = await action_prepare(
            channel_id=channel_id,
            agent_id=agent_id,
            agent_name=agent_name,
            action=args["action"],
            details=args["details"],
            gateway=gateway,
        )
        card_id = result["card_id"]
        approval = await action_wait(card_id, timeout=300)
        if approval == "approved":
            return f"Action approved by human: {args['action']}"
        elif approval == "timeout":
            return f"Action timed out waiting for human approval: {args['action']}"
        return f"Action rejected by human: {args['action']}"

    return f"Unknown action tool: {fn_name}"
