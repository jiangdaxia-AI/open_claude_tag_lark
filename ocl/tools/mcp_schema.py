"""MCP tool schema → LiteLLM function-tool conversion.

MCP ``tools/list`` returns entries shaped like::

    {"name": "read_file", "description": "...", "inputSchema": {...}}

LiteLLM / OpenAI function-calling wants::

    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": ...}}

Tool names are prefixed ``mcp__{server_name}__{tool_name}`` so they cannot
collide with built-in / feishu_doc tools and so dispatch can route by prefix.
"""

from __future__ import annotations

MCP_TOOL_PREFIX = "mcp__"


def convert_mcp_tool(server_name: str, tool: dict) -> dict:
    """Convert one MCP tool entry to a LiteLLM function-tool schema."""
    tool_name = tool["name"]
    return {
        "type": "function",
        "function": {
            "name": f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}",
            "description": tool.get("description") or "",
            "parameters": tool.get("inputSchema") or {"type": "object"},
        },
    }


def split_mcp_tool_name(fn_name: str) -> tuple[str, str]:
    """Split ``mcp__{server}__{tool}`` into ``(server, tool)``.

    Raises ValueError if the name does not match the expected shape.
    """
    rest = fn_name[len(MCP_TOOL_PREFIX):]
    parts = rest.split("__", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid MCP tool name: {fn_name!r}")
    return parts[0], parts[1]
