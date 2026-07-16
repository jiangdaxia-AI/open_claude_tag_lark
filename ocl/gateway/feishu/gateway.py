"""FeishuGateway — implements Gateway protocol against Feishu OpenAPI.

Endpoints used:
- POST /open-apis/auth/v3/tenant_access_token/internal (via TokenManager)
- POST /open-apis/im/v1/messages (send_message)
- GET  /open-apis/contact/v3/users/{user_id} (get_user_name)
- GET  /open-apis/im/v1/chats/{chat_id}/members (get_chat_members)
- POST /open-apis/im/v1/messages/{message_id}/reactions (add_reaction)
- DELETE /open-apis/im/v1/messages/{message_id}/reactions/{reaction_id} (remove_reaction)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ocl.gateway.feishu.auth import TokenManager

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"
_MAX_MESSAGE_BYTES = 30_000  # leave headroom under Feishu's 30KB limit
_PARAGRAPH_BREAK = "\n\n"


class FeishuGateway:
    """Gateway implementation for Feishu (lark).

    Supports multi-bot mode: each agent can have its own Feishu app credentials.
    Register a per-agent TokenManager via register_agent_token_manager(); when
    sending messages with an agent_id, the corresponding bot's token is used so
    the message appears as sent by that bot.
    """

    def __init__(self, token_mgr: "TokenManager", tenant_id: str) -> None:
        self._token_mgr = token_mgr  # default (primary bot)
        self._tenant_id = tenant_id
        self._client = httpx.AsyncClient(timeout=30)
        # Per-agent token managers: agent_id → TokenManager
        self._agent_token_mgrs: dict[str, "TokenManager"] = {}
        # Cache reaction_id so remove_reaction can find it later.
        # Key: (message_id, emoji) → Value: reaction_id
        self._reaction_cache: dict[tuple[str, str], str] = {}

    def register_agent_token_manager(self, agent_id: str, token_mgr: "TokenManager") -> None:
        """Register a TokenManager for a specific agent (multi-bot mode)."""
        self._agent_token_mgrs[agent_id] = token_mgr

    def _get_token_mgr(self, agent_id: str | None = None) -> "TokenManager":
        """Return the TokenManager for the given agent, falling back to the primary."""
        if agent_id and agent_id in self._agent_token_mgrs:
            return self._agent_token_mgrs[agent_id]
        return self._token_mgr

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def close(self) -> None:
        await self._client.aclose()

    # ──────────────────────────── send_message ────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a message. Splits long text by paragraphs (≤30KB each)."""
        chunks = self._split_text(text)
        first_message_id = ""
        for i, chunk in enumerate(chunks):
            # M6 fix: Thread subsequent chunks under the first message_id
            current_reply_to = reply_to if i == 0 else first_message_id or None
            message_id = await self._post_message(chat_id, chunk, current_reply_to, agent_id=agent_id)
            if i == 0:
                first_message_id = message_id
        return first_message_id or message_id

    def _split_text(self, text: str) -> list[str]:
        """Split text into chunks each ≤30KB. Prefer paragraph boundaries."""
        text_bytes = text.encode("utf-8")
        if len(text_bytes) <= _MAX_MESSAGE_BYTES:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for paragraph in text.split(_PARAGRAPH_BREAK):
            para_size = len(paragraph.encode("utf-8")) + len(_PARAGRAPH_BREAK)
            if current_size + para_size > _MAX_MESSAGE_BYTES and current:
                chunks.append(_PARAGRAPH_BREAK.join(current))
                current = []
                current_size = 0
            current.append(paragraph)
            current_size += para_size
        if current:
            chunks.append(_PARAGRAPH_BREAK.join(current))
        logger.info("Split long message into %d chunks", len(chunks))
        return chunks

    async def _post_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None,
        agent_id: str | None = None,
    ) -> str:
        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        # I5 fix: Use json.dumps to avoid double encoding (was using f-string interpolation)
        payload: dict = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        params = {"receive_id_type": "chat_id"}
        if reply_to:
            payload["reply_in_thread"] = True
        resp = await self._client.post(
            f"{_BASE}/im/v1/messages",
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        msg_id = data["message_id"]
        logger.info("Sent message: chat_id=%s message_id=%s length=%d",
                    chat_id, msg_id, len(text))
        return msg_id

    # ──────────────────────────── send_message_with_mentions ────────────

    async def send_message_with_mentions(
        self,
        chat_id: str,
        text: str,
        mentions: list[dict],
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a rich-text message with real @mentions using Feishu 'post' format.

        - mentions: [{"open_id": "ou_xxx", "name": "一句话需求"}, ...]
        - text: the message body. @name placeholders in text are replaced with
          <at> tags so Feishu renders them as real mentions and triggers events.

        Feishu 'post' msg_type format:
          content = {"zh_cn": {"title": "...", "content": [[{tag}, {tag}], ...]}}
          Each content row is a list of content blocks. <at> blocks look like:
          {"tag": "at", "user_id": "ou_xxx", "user_name": "Name"}
        """
        # Build content blocks: split text by @name placeholders and interleave <at> blocks
        content_rows: list[list[dict]] = []
        remaining = text
        # Sort mentions by name length desc to avoid partial-match collisions
        sorted_mentions = sorted(mentions, key=lambda m: len(m.get("name", "")), reverse=True)

        current_row: list[dict] = []
        while remaining:
            # Find the earliest @name match among all mention names
            earliest_pos = -1
            matched_mention = None
            for m in sorted_mentions:
                name = m.get("name", "")
                if not name:
                    continue
                placeholder = f"@{name}"
                pos = remaining.find(placeholder)
                if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                    earliest_pos = pos
                    matched_mention = m

            if earliest_pos == -1 or matched_mention is None:
                # No more mentions — flush remaining text
                if remaining.strip():
                    current_row.append({"tag": "text", "text": remaining})
                break

            # Text before the mention
            before = remaining[:earliest_pos]
            if before:
                current_row.append({"tag": "text", "text": before})

            # The <at> block
            current_row.append({
                "tag": "at",
                "user_id": matched_mention["open_id"],
                "user_name": matched_mention.get("name", ""),
            })

            # Advance past the @name placeholder
            name = matched_mention.get("name", "")
            remaining = remaining[earliest_pos + len(name) + 1:]  # +1 for the @

        if current_row:
            content_rows.append(current_row)

        if not content_rows:
            # Fallback: plain text if no content was built
            content_rows.append([{"tag": "text", "text": text or " "}])

        post_content = {
            "zh_cn": {
                "title": "",
                "content": content_rows,
            }
        }

        payload: dict = {
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(post_content),
        }
        params = {"receive_id_type": "chat_id"}
        if reply_to:
            payload["reply_in_thread"] = True

        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        resp = await self._client.post(
            f"{_BASE}/im/v1/messages",
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        # File-based debug log — bypasses structlog
        import time as _time
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"[{_time.time()}] send_message_with_mentions: HTTP {resp.status_code}\n")
            _f.write(f"  payload={json.dumps(payload, ensure_ascii=False)[:300]}\n")
            _f.write(f"  resp_body={resp.text[:500]}\n")
        resp.raise_for_status()
        data = resp.json()["data"]
        msg_id = data["message_id"]
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"  SUCCESS message_id={msg_id}\n")
        logger.info(
            "Sent message with %d mentions: chat_id=%s message_id=%s",
            len(mentions), chat_id, msg_id,
        )
        return msg_id

    # ──────────────────────────── streaming card support ────────────────

    async def send_card_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a card message (for streaming — cards PATCH-update instantly in Feishu client).

        Uses div + lark_md format with config block for correct PATCH rendering.
        Returns the message_id for subsequent update_card_message calls.
        """
        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        card = self._build_card(text)
        payload: dict = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        params = {"receive_id_type": "chat_id"}
        if reply_to:
            payload["reply_in_thread"] = True
        resp = await self._client.post(
            f"{_BASE}/im/v1/messages",
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        msg_id = resp.json()["data"]["message_id"]
        import time as _t
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"  [{_t.time()}] send_card POST msg={msg_id} text={text[:80]!r}\n")
        return msg_id

    @staticmethod
    def _build_card(text: str) -> dict:
        """Build a Feishu interactive card with a single div + plain_text block.

        Uses plain_text (not lark_md) because lark_md causes content duplication
        on PATCH updates in the Feishu client. plain_text replaces cleanly.
        """
        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": text or " "}},
            ],
        }

    async def update_card_message(
        self,
        message_id: str,
        text: str,
        agent_id: str | None = None,
    ) -> None:
        """Update a card message's content via Feishu PATCH API.

        Feishu client renders card updates instantly (unlike text message PATCH
        which has rendering delays). This is what makes streaming feel live.
        """
        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        card = self._build_card(text)
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        try:
            resp = await self._client.patch(
                f"{_BASE}/im/v1/messages/{message_id}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            # Debug: log what we're actually sending
            import time as _t
            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"  [{_t.time()}] update_card PATCH msg={message_id} text={text[:80]!r}\n")
            if resp.status_code != 200:
                logger.debug("update_card_message HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("update_card_message failed: %s", e)

    # ──────────────────────────── update_message (PATCH, text) ────────────

    async def update_message(
        self,
        message_id: str,
        text: str,
        agent_id: str | None = None,
    ) -> None:
        """Update an existing text message's content via Feishu PATCH API."""
        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        try:
            resp = await self._client.patch(
                f"{_BASE}/im/v1/messages/{message_id}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.debug("update_message HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("update_message failed: %s", e)

    # ──────────────────────────── file upload ────────────────────────────

    async def upload_file(
        self,
        chat_id: str,
        file_path: str,
        file_name: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Upload a file to a Feishu chat. Returns the file_key.

        Uses the Feishu im/v1/files API to upload, then optionally sends
        a file message to the chat.
        """
        import os as _os
        token_mgr = self._get_token_mgr(agent_id)
        token = await token_mgr.get_tenant_token()
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(file_path)
        fname = file_name or path.name

        # Upload file
        with open(path, "rb") as f:
            resp = await self._client.post(
                f"{_BASE}/im/v1/files",
                params={"file_type": "stream", "file_name": fname},
                files={"file": (fname, f, "application/octet-stream")},
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        file_key = resp.json()["data"]["file_key"]

        # Send file message to chat
        payload = {
            "receive_id": chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        }
        resp = await self._client.post(
            f"{_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        msg_id = resp.json()["data"]["message_id"]
        logger.info("Uploaded file %s to chat %s, message_id=%s", fname, chat_id, msg_id)
        return msg_id

    # ──────────────────────────── get_user_name ───────────────────────────

    async def get_user_name(self, user_id: str) -> str:
        token = await self._token_mgr.get_tenant_token()
        try:
            resp = await self._client.get(
                f"{_BASE}/contact/v3/users/{user_id}",
                params={"user_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("get_user_name failed: status=%s; falling back", resp.status_code)
                return user_id
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("get_user_name API error: code=%s; falling back", data.get("code"))
                return user_id
            user = data["data"]["user"]
            return user.get("name") or user.get("en_name") or user_id
        except Exception as e:
            logger.warning("get_user_name exception: %s; falling back to user_id", e)
            return user_id

    # ──────────────────────────── get_chat_members ────────────────────────

    async def get_chat_members(self, chat_id: str) -> dict[str, str]:
        token = await self._token_mgr.get_tenant_token()
        result: dict[str, str] = {}
        page_token = ""
        try:
            while True:
                params: dict = {"member_id_type": "open_id", "page_size": 50}
                if page_token:
                    params["page_token"] = page_token
                resp = await self._client.get(
                    f"{_BASE}/im/v1/chats/{chat_id}/members",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    logger.warning("get_chat_members HTTP %s; returning partial", resp.status_code)
                    break
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning("get_chat_members API error: code=%s", data.get("code"))
                    break
                for m in data["data"].get("member_list") or []:
                    result[m["member_id"]] = m.get("name") or m["member_id"]
                if not data["data"].get("has_more"):
                    break
                page_token = data["data"].get("page_token") or ""
            return result
        except Exception as e:
            logger.warning("get_chat_members exception: %s; returning %d members", e, len(result))
            return result

    # ──────────────────────────── reactions ───────────────────────────────

    async def add_reaction(self, message_id: str, emoji: str) -> None:
        token = await self._token_mgr.get_tenant_token()
        try:
            resp = await self._client.post(
                f"{_BASE}/im/v1/messages/{message_id}/reactions",
                json={"reaction_type": {"emoji_type": emoji}},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("add_reaction HTTP %s; swallowed", resp.status_code)
                return
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("add_reaction API error: code=%s; swallowed", data.get("code"))
                return
            reaction_id = data["data"]["reaction_id"]
            self._reaction_cache[(message_id, emoji)] = reaction_id
        except Exception as e:
            logger.warning("add_reaction exception: %s; swallowed", e)

    async def remove_reaction(self, message_id: str, emoji: str) -> None:
        reaction_id = self._reaction_cache.get((message_id, emoji))
        if not reaction_id:
            logger.warning("remove_reaction: no cached reaction_id for %s/%s", message_id, emoji)
            return
        token = await self._token_mgr.get_tenant_token()
        try:
            resp = await self._client.delete(
                f"{_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("remove_reaction HTTP %s; swallowed", resp.status_code)
                return
            self._reaction_cache.pop((message_id, emoji), None)
        except Exception as e:
            logger.warning("remove_reaction exception: %s; swallowed", e)
