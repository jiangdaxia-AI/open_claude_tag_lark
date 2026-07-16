"""Feishu tenant_access_token manager + bot info fetcher.

Caches the token in-memory. Refreshes 5 minutes before expiry.
No locks, no retries — failures propagate as httpx.HTTPStatusError so the
caller's existing try/except can surface them.

Also provides fetch_bot_open_id() — uses app_id + app_secret to call
the Feishu bot info API and auto-discover the bot's open_id, so users
don't need to manually look it up.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_REFRESH_THRESHOLD_SECONDS = 300  # refresh if less than 5 minutes left
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_BOT_INFO_URL = "https://open.feishu.cn/open-apis/bot/v3/info/"


class TokenManager:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._client = httpx.AsyncClient(timeout=10)

    async def get_tenant_token(self) -> str:
        """Return a valid tenant_access_token, refreshing if needed."""
        now = time.time()
        if self._token and now < self._expires_at - _REFRESH_THRESHOLD_SECONDS:
            return self._token

        resp = await self._client.post(
            _TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["tenant_access_token"]
        self._expires_at = now + data["expire"]
        logger.info("Feishu token refreshed; expires_in=%s", data["expire"])
        return self._token

    async def fetch_bot_open_id(self) -> str:
        """Fetch this app's bot open_id via the bot info API.

        Requires a valid tenant_access_token (will fetch one if needed).
        Returns the open_id string, or "" on failure.
        """
        try:
            token = await self.get_tenant_token()
            resp = await self._client.get(
                _BOT_INFO_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            bot = data.get("bot", {})
            open_id = bot.get("open_id", "")
            if open_id:
                logger.info(
                    "Auto-discovered bot open_id=%s (name=%s)",
                    open_id,
                    bot.get("bot_name", "<unknown>"),
                )
            return open_id
        except Exception as exc:
            logger.warning("Failed to fetch bot open_id: %s", exc)
            return ""

    async def close(self) -> None:
        await self._client.aclose()
