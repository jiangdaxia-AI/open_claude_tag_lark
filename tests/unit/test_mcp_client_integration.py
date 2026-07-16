"""Integration test: MCPClientManager against a real stdio subprocess (fake server)."""

import sys
from pathlib import Path

import pytest

from ocl.tools.mcp_client import MCPClientManager

_FAKE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_mcp_server.py")
_CHANNEL = "C_test"


@pytest.fixture
def manager_with_fake():
    mgr = MCPClientManager()
    mgr.set_channel_configs({
        _CHANNEL: [
            {
                "name": "fake",
                "transport": "stdio",
                "command": sys.executable,
                "args": [_FAKE_SERVER],
                "env": {},
            }
        ]
    })
    yield mgr
    # cleanup best-effort (loop may already be closing in some test runners)


async def test_list_tools_returns_echo_tool(manager_with_fake):
    tools = await manager_with_fake.list_tools(_CHANNEL)
    names = [t["function"]["name"] for t in tools]
    assert "mcp__fake__echo" in names


async def test_get_cached_tools_empty_before_warm(manager_with_fake):
    assert manager_with_fake.get_cached_tools(_CHANNEL) == []


async def test_warm_tools_populates_cache(manager_with_fake):
    await manager_with_fake.warm_tools(_CHANNEL)
    cached = manager_with_fake.get_cached_tools(_CHANNEL)
    assert any(t["function"]["name"] == "mcp__fake__echo" for t in cached)


async def test_call_tool_returns_echoed_text(manager_with_fake):
    result = await manager_with_fake.call_tool(_CHANNEL, "mcp__fake__echo", {"text": "hello"})
    assert result == "hello"


async def test_call_tool_persistent_connection(manager_with_fake):
    """Two calls reuse the same session (no re-connect)."""
    await manager_with_fake.call_tool(_CHANNEL, "mcp__fake__echo", {"text": "one"})
    session_key = (_CHANNEL, "fake")
    session1 = manager_with_fake._sessions.get(session_key)
    await manager_with_fake.call_tool(_CHANNEL, "mcp__fake__echo", {"text": "two"})
    session2 = manager_with_fake._sessions.get(session_key)
    assert session1 is not None and session2 is not None
    # same session object reused
    assert session1[0] is session2[0]


async def test_call_tool_unknown_returns_error_string(manager_with_fake):
    result = await manager_with_fake.call_tool(_CHANNEL, "mcp__nonexistent__x", {})
    assert isinstance(result, str)
    assert "nonexistent" in result or "not found" in result.lower() or "failed" in result.lower()


async def test_shutdown_closes_sessions(manager_with_fake):
    await manager_with_fake.call_tool(_CHANNEL, "mcp__fake__echo", {"text": "x"})
    await manager_with_fake.shutdown()
    assert manager_with_fake._sessions == {}
