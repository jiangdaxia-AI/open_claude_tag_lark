"""Per-channel lazy persistent MCP client manager.

Holds one ``ClientSession`` per (channel_id, server_name). Connections are
established lazily on first use and reused until they error or the manager
shuts down. Uses ``contextlib.AsyncExitStack`` to keep the stdio / http
transport context managers alive for the lifetime of each session.

Tool discovery (``tools/list``) results are cached per channel and exposed
synchronously via ``get_cached_tools`` so the sync ``get_channel_tools``
in registry.py can merge them. The agent loop warms the cache with
``warm_tools`` at the start of each turn.

All errors surface as plain strings (never raise into the agent loop).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ocl.tools.mcp_schema import convert_mcp_tool, split_mcp_tool_name

logger = logging.getLogger(__name__)

# (channel_id, server_name) -> (session, exit_stack)  — exit_stack keeps transport alive
_SessionEntry = tuple[ClientSession, contextlib.AsyncExitStack]


class MCPClientManager:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], _SessionEntry] = {}
        self._tool_cache: dict[str, list[dict]] = {}
        self._configs: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    def set_channel_configs(self, configs: dict[str, list[dict]]) -> None:
        self._configs = dict(configs)

    def get_cached_tools(self, channel_id: str) -> list[dict]:
        return list(self._tool_cache.get(channel_id, []))

    async def warm_tools(self, channel_id: str) -> None:
        """Populate the tool cache for a channel (best-effort)."""
        try:
            self._tool_cache[channel_id] = await self.list_tools(channel_id)
        except Exception:
            logger.exception("warm_tools failed for channel=%s", channel_id)

    async def list_tools(self, channel_id: str) -> list[dict]:
        servers = self._configs.get(channel_id, [])
        out: list[dict] = []
        for cfg in servers:
            session = await self.ensure_connected(channel_id, cfg["name"])
            if session is None:
                continue
            try:
                result = await session.list_tools()
            except Exception:
                logger.exception("list_tools failed for server=%s channel=%s", cfg["name"], channel_id)
                self._drop_session(channel_id, cfg["name"])
                continue
            for tool in result.tools:
                out.append(convert_mcp_tool(cfg["name"], {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                }))
        return out

    async def call_tool(self, channel_id: str, fn_name: str, args: dict[str, Any]) -> str:
        try:
            server_name, tool_name = split_mcp_tool_name(fn_name)
        except ValueError:
            return f"MCP tool name {fn_name!r} is malformed."
        session = await self.ensure_connected(channel_id, server_name)
        if session is None:
            return f"MCP server {server_name!r} is not available."
        try:
            result = await session.call_tool(tool_name, args)
        except Exception as exc:
            logger.exception("call_tool failed: %s in channel=%s", fn_name, channel_id)
            self._drop_session(channel_id, server_name)
            return f"MCP tool {fn_name!r} failed: {exc}"

        if result.isError:
            text = _extract_text(result.content)
            return f"MCP tool {fn_name!r} returned an error: {text}"

        return _extract_text(result.content)

    async def ensure_connected(self, channel_id: str, server_name: str) -> ClientSession | None:
        key = (channel_id, server_name)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing[0]

        async with self._lock:
            # double-check after acquiring lock
            existing = self._sessions.get(key)
            if existing is not None:
                return existing[0]

            cfg = self._find_config(channel_id, server_name)
            if cfg is None:
                logger.warning("No MCP config for server=%s channel=%s", server_name, channel_id)
                return None

            entry = await self._connect(cfg)
            if entry is None:
                return None
            self._sessions[key] = entry
            return entry[0]

    async def _connect(self, cfg: dict) -> _SessionEntry | None:
        stack = contextlib.AsyncExitStack()
        try:
            if cfg["transport"] == "stdio":
                env = {**__import__("os").environ, **cfg.get("env", {})}
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=list(cfg.get("args", [])),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:  # http
                read, write, _get_session_id = await stack.enter_async_context(
                    streamablehttp_client(cfg["url"], headers=cfg.get("headers"))
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            logger.info("MCP connected: server=%s transport=%s", cfg["name"], cfg["transport"])
            return session, stack
        except Exception:
            logger.exception("Failed to connect MCP server=%s", cfg.get("name"))
            await stack.aclose()
            return None

    def _find_config(self, channel_id: str, server_name: str) -> dict | None:
        for cfg in self._configs.get(channel_id, []):
            if cfg["name"] == server_name:
                return cfg
        return None

    def _drop_session(self, channel_id: str, server_name: str) -> None:
        key = (channel_id, server_name)
        entry = self._sessions.pop(key, None)
        if entry is not None:
            _, stack = entry
            # best-effort close; don't block dispatch on cleanup
            asyncio.ensure_future(stack.aclose())

    async def shutdown(self) -> None:
        entries = list(self._sessions.values())
        self._sessions.clear()
        for _, stack in entries:
            try:
                await stack.aclose()
            except Exception:
                logger.exception("Error closing MCP session during shutdown")


def _extract_text(content_blocks: list) -> str:
    """Join text content blocks into a single string."""
    parts: list[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts) if parts else "(no output)"
