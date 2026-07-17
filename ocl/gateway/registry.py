"""Gateway registry — select platform adapter by config.

Currently only Feishu is implemented. To add a new platform:

1. Create a new directory: ocl/gateway/<platform>/
2. Implement the Gateway protocol (see ocl/gateway/base.py) in a Gateway class
3. Add an entry to _ADAPTERS below
4. Set GATEWAY_PLATFORM=<platform> in .env

The agent loop (ocl/agent/loop.py) only depends on the Gateway protocol,
so it works unchanged with any platform adapter.
"""

from __future__ import annotations

from typing import Any

_ADAPTERS: dict[str, str] = {
    "feishu": "ocl.gateway.feishu.gateway.FeishuGateway",
    # "discord": "ocl.gateway.discord.gateway.DiscordGateway",  # TODO: implement
    # "teams": "ocl.gateway.teams.gateway.TeamsGateway",        # TODO: implement
}


def get_gateway_class(platform: str = "feishu") -> type:
    """Import and return the Gateway class for the given platform.

    Raises ImportError if the platform adapter is not installed.
    """
    class_path = _ADAPTERS.get(platform)
    if not class_path:
        raise ImportError(
            f"Unknown gateway platform: {platform!r}. "
            f"Available: {list(_ADAPTERS.keys())}"
        )
    module_path, class_name = class_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def list_platforms() -> list[dict[str, str]]:
    """Return list of supported platforms with their implementation status."""
    result = []
    for platform, class_path in _ADAPTERS.items():
        try:
            get_gateway_class(platform)
            status = "available"
        except ImportError:
            status = "not_implemented"
        result.append({"platform": platform, "status": status, "class": class_path})
    return result
