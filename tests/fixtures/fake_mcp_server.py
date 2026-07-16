"""Minimal stdio MCP server for integration tests.

Speaks the MCP protocol via stdin/stdout using the official `mcp` SDK's
FastMCP. Run as a subprocess: ``python fake_mcp_server.py``. Exposes one
tool ``echo`` that returns its argument unchanged.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return text


if __name__ == "__main__":
    mcp.run()
