"""Tests for tools.toml [[mcp_server]] parsing."""

import os
from unittest.mock import patch

from ocl.tools.mcp_config import parse_mcp_servers, load_all_channel_mcp_configs


def test_parse_stdio_server():
    toml = """
[[mcp_server]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
"""
    servers = parse_mcp_servers(toml)
    assert len(servers) == 1
    s = servers[0]
    assert s["name"] == "filesystem"
    assert s["transport"] == "stdio"
    assert s["command"] == "npx"
    assert s["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    assert s.get("env", {}) == {}


def test_parse_http_server_with_env_interpolation(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret123")
    toml = """
[[mcp_server]]
name = "github"
transport = "http"
url = "https://mcp.github.com/sse"
headers = { Authorization = "Bearer ${GH_TOKEN}" }
timeout_seconds = 45
"""
    servers = parse_mcp_servers(toml)
    s = servers[0]
    assert s["transport"] == "http"
    assert s["url"] == "https://mcp.github.com/sse"
    assert s["headers"] == {"Authorization": "Bearer secret123"}
    assert s["timeout_seconds"] == 45


def test_env_var_missing_replaced_with_empty(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    toml = """
[[mcp_server]]
name = "s"
transport = "http"
url = "https://x"
headers = { X = "${MISSING_VAR}" }
"""
    servers = parse_mcp_servers(toml)
    assert servers[0]["headers"] == {"X": ""}


def test_parse_rejects_missing_required_field():
    toml = """
[[mcp_server]]
name = "broken"
transport = "stdio"
"""
    servers = parse_mcp_servers(toml)
    assert servers == []  # invalid entry dropped, not raised


def test_parse_rejects_unknown_transport():
    toml = """
[[mcp_server]]
name = "x"
transport = "websocket"
command = "foo"
"""
    servers = parse_mcp_servers(toml)
    assert servers == []


def test_load_all_channel_configs(tmp_path, monkeypatch):
    # channels_dir is a @property = data_dir / "channels", so patch data_dir
    # and create the channels/ subdir.
    channels = tmp_path / "channels"
    channels.mkdir()
    (channels / "C001").mkdir()
    (channels / "C001" / "tools.toml").write_text(
        '[[mcp_server]]\nname = "fs"\ntransport = "stdio"\ncommand = "cat"\n'
    )
    (channels / "C002").mkdir()  # no tools.toml — skipped

    from ocl import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)

    configs = load_all_channel_mcp_configs()
    assert set(configs.keys()) == {"C001"}
    assert configs["C001"][0]["name"] == "fs"
