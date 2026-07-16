"""Tool registry — loads allowed tools per channel AND per agent from tools.toml.

All tools are exposed as MCP-compatible function schemas so the agent can call them.
Built-in tools are always available. Channel admins can add MCP servers via tools.toml.
Per-agent tools.toml at channels/<id>/agents/<agent_id>/tools.toml overrides
channel-level config for that specific agent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import toml

from ocl.config import settings
from ocl.tools.builtins import BUILTIN_TOOLS, dispatch_builtin
from ocl.tools.feishu_docs import FEISHU_DOC_TOOLS, dispatch_feishu_doc

if TYPE_CHECKING:
    from ocl.memory.store import MessageStore

logger = logging.getLogger(__name__)

_token_mgr = None
_mcp_mgr = None


def set_token_mgr(token_mgr) -> None:
    """Inject the Feishu TokenManager so feishu_doc tools can fetch tokens."""
    global _token_mgr
    _token_mgr = token_mgr


def set_mcp_mgr(mgr) -> None:
    """Inject the MCPClientManager so mcp__* tools can be dispatched."""
    global _mcp_mgr
    _mcp_mgr = mgr


def get_channel_tools(channel_id: str, agent_id: str = "default") -> list[dict]:
    """Return LiteLLM-compatible tool schemas for this channel + agent.

    Loads channel-level tools.toml first, then merges per-agent tools.toml.
    """
    tools = list(BUILTIN_TOOLS)
    tools.extend(FEISHU_DOC_TOOLS)

    # Channel-level tools
    _load_tools_from_path(tools, settings.channels_dir / channel_id / "tools.toml")

    # Per-agent tools (overrides channel-level for MCP servers)
    if agent_id and agent_id != "default":
        agent_path = settings.channels_dir / channel_id / "agents" / agent_id / "tools.toml"
        _load_tools_from_path(tools, agent_path)

    if _mcp_mgr is not None:
        tools.extend(_mcp_mgr.get_cached_tools(channel_id))
    return tools


def _load_tools_from_path(tools: list[dict], path: Path) -> None:
    """Parse a tools.toml and note MCP server registrations."""
    if not path.exists():
        return
    try:
        config = toml.loads(path.read_text(encoding="utf-8"))
        for server in config.get("mcp_server", []):
            logger.debug("MCP server registered: %s", server.get("name"))
    except Exception:
        logger.exception("Failed to parse %s", path)


async def dispatch_tool(
    fn_name: str,
    args: dict[str, Any],
    channel_id: str,
    store: "MessageStore | None" = None,
    agent_id: str = "default",
    user_id: str = "",
) -> Any:
    """Dispatch a tool call to built-ins, feishu_doc tools, or MCP servers."""
    # search_channel_history needs the per-channel MessageStore
    if fn_name == "search_channel_history":
        if store is None:
            return "Channel history search is unavailable (no message store)."
        query = args.get("query", "")
        if not query:
            return "No query provided."
        rows = await store.search(query)
        return _format_search_results(query, rows)

    if fn_name in {t["function"]["name"] for t in BUILTIN_TOOLS}:
        return await dispatch_builtin(fn_name, args, channel_id=channel_id, agent_id=agent_id, user_id=user_id, store=store)

    if fn_name in {t["function"]["name"] for t in FEISHU_DOC_TOOLS}:
        if _token_mgr is None:
            return "Feishu doc tools not available: token manager not configured"
        return await dispatch_feishu_doc(fn_name, args, token_mgr=_token_mgr)

    if fn_name.startswith("mcp__"):
        if _mcp_mgr is None:
            return "MCP tools not available."
        try:
            return await _mcp_mgr.call_tool(channel_id, fn_name, args)
        except Exception as exc:
            logger.exception("MCP dispatch raised unexpectedly for %s", fn_name)
            return f"MCP tool {fn_name!r} failed: {exc}"

    # Memory tools are handled directly in the agent loop
    if fn_name in ("memory_append", "memory_replace", "memory_delete"):
        return None

    logger.warning("Unknown tool: %s in channel=%s", fn_name, channel_id)
    return f"Tool '{fn_name}' not found."


def _format_search_results(query: str, rows: list) -> str:
    if not rows:
        return f"No messages matched '{query}'."
    lines = [f"Found {len(rows)} matching message(s) for '{query}':"]
    for r in rows:
        lines.append(f"- [{r['display_name']}] {r['content']}")
    return "\n".join(lines)
