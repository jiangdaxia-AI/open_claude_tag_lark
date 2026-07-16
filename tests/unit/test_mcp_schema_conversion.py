"""Tests for MCP tool → LiteLLM schema conversion."""

from ocl.tools.mcp_schema import convert_mcp_tool, split_mcp_tool_name, MCP_TOOL_PREFIX


def test_convert_adds_mcp_prefix():
    mcp_tool = {
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    result = convert_mcp_tool("filesystem", mcp_tool)
    assert result == {
        "type": "function",
        "function": {
            "name": "mcp__filesystem__read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }


def test_convert_handles_missing_description():
    mcp_tool = {"name": "ping", "inputSchema": {"type": "object"}}
    result = convert_mcp_tool("srv", mcp_tool)
    assert result["function"]["name"] == "mcp__srv__ping"
    assert result["function"]["description"] == ""
    assert result["function"]["parameters"] == {"type": "object"}


def test_split_mcp_tool_name():
    server, tool = split_mcp_tool_name("mcp__filesystem__read_file")
    assert server == "filesystem"
    assert tool == "read_file"


def test_mcp_tool_prefix_is_mcp_double_underscore():
    assert MCP_TOOL_PREFIX == "mcp__"
