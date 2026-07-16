"""Tests for the manager-construction helper extracted from ws_client.start()."""

from unittest.mock import MagicMock

from ocl.gateway.feishu.ws_client import _build_managers


def test_build_managers_returns_all_three():
    token_mgr = MagicMock()
    gateway = MagicMock()
    gateway.tenant_id = "T1"
    import asyncio
    bg_loop = asyncio.new_event_loop()

    mcp_mgr, heartbeat, scheduler = _build_managers(token_mgr, gateway, bg_loop)

    from ocl.tools.registry import _mcp_mgr as registry_mgr
    # set_mcp_mgr was called inside _build_managers
    assert mcp_mgr is not None
    assert heartbeat is not None
    assert scheduler is not None
    bg_loop.close()


def test_build_managers_injects_mcp_mgr_into_registry():
    token_mgr = MagicMock()
    gateway = MagicMock()
    gateway.tenant_id = "T1"
    import asyncio
    bg_loop = asyncio.new_event_loop()

    mcp_mgr, _, _ = _build_managers(token_mgr, gateway, bg_loop)

    from ocl.tools import registry
    assert registry._mcp_mgr is mcp_mgr
    bg_loop.close()
