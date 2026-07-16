"""Parse ``[[mcp_server]]`` entries from per-channel ``tools.toml``.

Validated server config dict shape::

    {"name": str, "transport": "stdio" | "http",
     # stdio: "command": str, "args": list[str], "env": dict[str,str]
     # http:  "url": str, "headers": dict[str,str], "timeout_seconds": int}

``${VAR}`` in http header values is expanded from ``os.environ`` at load time
so secrets never live in the config file.
"""

from __future__ import annotations

import logging
import os
import re

import toml

from ocl.config import settings

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VALID_TRANSPORTS = ("stdio", "http")


def parse_mcp_servers(toml_text: str) -> list[dict]:
    """Parse tools.toml text into a list of validated server configs.

    Invalid entries are logged and dropped (never raised) so one bad
    channel config cannot crash bot startup.
    """
    try:
        config = toml.loads(toml_text)
    except Exception:
        logger.exception("Failed to parse tools.toml")
        return []

    seen_names: set[str] = set()
    servers: list[dict] = []
    for raw in config.get("mcp_server", []):
        server = _validate_one(raw)
        if server is None:
            continue
        if server["name"] in seen_names:
            logger.warning("Duplicate MCP server name %r — latter overrides", server["name"])
            servers = [s for s in servers if s["name"] != server["name"]]
        seen_names.add(server["name"])
        servers.append(server)
    return servers


def _validate_one(raw: dict) -> dict | None:
    name = raw.get("name")
    transport = raw.get("transport")
    if not name or not isinstance(name, str):
        logger.warning("MCP server entry missing 'name': %r", raw)
        return None
    if transport not in _VALID_TRANSPORTS:
        logger.warning("MCP server %r has invalid transport %r", name, transport)
        return None

    if transport == "stdio":
        command = raw.get("command")
        if not command or not isinstance(command, str):
            logger.warning("stdio MCP server %r missing 'command'", name)
            return None
        args = raw.get("args", [])
        if not isinstance(args, list):
            logger.warning("MCP server %r 'args' must be a list", name)
            return None
        env = raw.get("env", {})
        if not isinstance(env, dict):
            logger.warning("MCP server %r 'env' must be a dict", name)
            return None
        return {"name": name, "transport": "stdio", "command": command, "args": list(args), "env": dict(env)}

    # http
    url = raw.get("url")
    if not url or not isinstance(url, str):
        logger.warning("http MCP server %r missing 'url'", name)
        return None
    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict):
        logger.warning("MCP server %r 'headers' must be a dict", name)
        return None
    headers = {str(k): _expand_env(str(v)) for k, v in headers_raw.items()}
    timeout = raw.get("timeout_seconds", 30)
    if not isinstance(timeout, int):
        timeout = 30
    return {"name": name, "transport": "http", "url": url, "headers": headers, "timeout_seconds": timeout}


def _expand_env(value: str) -> str:
    """Replace ``${VAR}`` with os.environ value (empty string if unset)."""
    def _sub(match: re.Match) -> str:
        var = match.group(1)
        val = os.environ.get(var)
        if val is None:
            logger.warning("Env var %r referenced in MCP config is not set", var)
            return ""
        return val
    return _ENV_VAR_RE.sub(_sub, value)


def load_all_channel_mcp_configs() -> dict[str, list[dict]]:
    """Scan ``channels/*/tools.toml`` and return ``{channel_id: [server_config, ...]}``."""
    result: dict[str, list[dict]] = {}
    if not settings.channels_dir.exists():
        return result
    for channel_dir in sorted(settings.channels_dir.iterdir()):
        if not channel_dir.is_dir():
            continue
        toml_path = channel_dir / "tools.toml"
        if not toml_path.exists():
            continue
        servers = parse_mcp_servers(toml_path.read_text())
        if servers:
            result[channel_dir.name] = servers
    return result
