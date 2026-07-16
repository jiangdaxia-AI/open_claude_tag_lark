"""Tests for channel router — verifies channel-scoped session isolation."""

from ocl.gateway.router import get_or_create_session, _sessions


def setup_function():
    _sessions.clear()


def test_same_channel_returns_same_session():
    s1 = get_or_create_session("W001", "C001")
    s2 = get_or_create_session("W001", "C001")
    assert s1 is s2


def test_different_channels_return_different_sessions():
    s1 = get_or_create_session("W001", "C001")
    s2 = get_or_create_session("W001", "C002")
    assert s1 is not s2


def test_different_workspaces_return_different_sessions():
    s1 = get_or_create_session("W001", "C001")
    s2 = get_or_create_session("W002", "C001")
    assert s1 is not s2


def test_session_has_channel_scoped_identity():
    s = get_or_create_session("W001", "C001")
    assert s.workspace_id == "W001"
    assert s.channel_id == "C001"


def test_route_message_signature_uses_gateway_not_app():
    import inspect
    from ocl.gateway.router import route_message
    sig = inspect.signature(route_message)
    assert "gateway" in sig.parameters
    assert "app" not in sig.parameters


async def test_get_session_lock_returns_lock_object():
    import asyncio
    from ocl.gateway.router import get_session_lock
    lock = get_session_lock("W001", "C001")
    assert isinstance(lock, asyncio.Lock)


async def test_get_session_lock_returns_same_lock_as_session():
    from ocl.gateway.router import get_session_lock, get_or_create_session
    lock = get_session_lock("W001", "C001")
    session = get_or_create_session("W001", "C001")
    assert lock is session._lock
