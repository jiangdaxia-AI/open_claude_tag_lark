"""Tests for FeishuEventHandler — event filtering, idempotency, routing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ocl.gateway.feishu.events import FeishuEventHandler


def _build_event(
    *,
    event_id: str = "evt-1",
    chat_type: str = "group",
    chat_id: str = "oc_test",
    message_id: str = "om_test",
    sender_id: str = "ou_alice",
    text: str = "hello",
    mentions: list[dict] | None = None,
) -> dict:
    msg_content = {"text": text}
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_id": {"open_id": sender_id}},
            "message": {
                "chat_type": chat_type,
                "chat_id": chat_id,
                "message_id": message_id,
                "message_type": "text",
                "content": __import__("json").dumps(msg_content),
                "mentions": mentions or [],
            },
        },
    }


def _make_handler(bot_open_id="ou_bot"):
    """Build a handler with a mocked gateway and mocked route_message."""
    gateway = MagicMock()
    gateway.add_reaction = AsyncMock()
    gateway.remove_reaction = AsyncMock()
    gateway.get_user_name = AsyncMock(return_value="Alice")
    handler = FeishuEventHandler(
        gateway=gateway,
        bot_open_id=bot_open_id,
        tenant_id="tenant-001",
    )
    return handler, gateway


@pytest.mark.asyncio
async def test_group_message_without_mention_is_ignored():
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(chat_type="group", mentions=[])
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    mock_route.assert_not_called()


@pytest.mark.asyncio
async def test_group_message_with_mention_routes():
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(
        chat_type="group",
        mentions=[{"id": {"open_id": "ou_bot"}, "name": "Agent", "key": "@_user_1"}],
    )
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    mock_route.assert_called_once()


@pytest.mark.asyncio
async def test_p2p_message_always_routes_regardless_of_mention():
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(chat_type="p2p", mentions=[])
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    mock_route.assert_called_once()


@pytest.mark.asyncio
async def test_duplicate_event_id_is_ignored():
    handler, gateway = _make_handler()
    event = _build_event(event_id="evt-dup")
    with patch.object(handler, "_is_processed", new=AsyncMock(return_value=True)), \
         patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route:
        await handler.on_message_receive(event)
    mock_route.assert_not_called()


@pytest.mark.asyncio
async def test_at_tag_is_cleaned_before_routing():
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    raw_text = '<at user_id="ou_bot">Agent</at> 帮我查 PR'
    event = _build_event(chat_type="p2p", text=raw_text)
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    routed_text = mock_route.call_args.kwargs.get("text", "")
    assert "@Agent" in routed_text
    assert "<at" not in routed_text


@pytest.mark.asyncio
async def test_bot_own_message_is_ignored():
    """Critical 2 fix: Bot loop prevention - ignore messages sent by the bot itself."""
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(sender_id="ou_bot", chat_type="p2p")
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    mock_route.assert_not_called()


@pytest.mark.asyncio
async def test_group_message_processes_when_bot_open_id_is_unset():
    """Important fix: When bot_open_id is None/empty, group messages should still process."""
    handler, gateway = _make_handler(bot_open_id=None)
    event = _build_event(chat_type="group", mentions=[])
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)
    mock_route.assert_called_once()


@pytest.mark.asyncio
async def test_filtered_message_does_not_mark_processed():
    """Critical 1 fix: A filtered-out message should NOT call _mark_processed."""
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(chat_type="group", mentions=[])

    mock_mark = AsyncMock()
    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", mock_mark):
        await handler.on_message_receive(event)

    # Should not route (no mention)
    mock_route.assert_not_called()
    # Should NOT mark as processed (filtered out before idempotency check)
    mock_mark.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_chat_type_logs_debug_but_processes():
    """I4 fix: Unknown chat_type values should log a debug message but still process."""
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    event = _build_event(chat_type="unknown_type", text="test")

    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()):
        await handler.on_message_receive(event)

    # Should still route the message (unknown types are allowed)
    mock_route.assert_called_once()


@pytest.mark.asyncio
async def test_bot_open_id_none_logs_warning():
    """I2 fix: When bot_open_id is None, a warning should be logged (once)."""
    handler, gateway = _make_handler(bot_open_id=None)
    event = _build_event(chat_type="p2p", text="test")

    with patch("ocl.gateway.feishu.events.route_message", new=AsyncMock()) as mock_route, \
         patch.object(handler, "_is_processed", new=AsyncMock(return_value=False)), \
         patch.object(handler, "_mark_processed", new=AsyncMock()) as mock_mark:
        await handler.on_message_receive(event)

    # Should route (no bot loop prevention when bot_open_id is None)
    mock_route.assert_called_once()
    # Should have logged the warning
    assert handler._bot_open_id_warned is True


@pytest.mark.asyncio
async def test_chat_id_parameter_in_idempotency_methods():
    """C3 fix: _is_processed and _mark_processed should accept chat_id parameter."""
    handler, gateway = _make_handler(bot_open_id="ou_bot")

    # Mock the store getter to return a mock store
    mock_store = AsyncMock()
    mock_store.is_event_processed = AsyncMock(return_value=False)
    mock_store.mark_event_processed = AsyncMock()

    async def mock_store_getter(chat_id):
        return mock_store

    handler._store_getter = mock_store_getter

    # Test _is_processed with chat_id parameter
    result = await handler._is_processed("evt-1", chat_id="oc_custom")
    assert result is False
    mock_store.is_event_processed.assert_called_once_with("evt-1")

    # Test _mark_processed with chat_id parameter
    await handler._mark_processed("evt-1", event_type="im.message.receive_v1", chat_id="oc_custom")
    mock_store.mark_event_processed.assert_called_once_with("evt-1", "im.message.receive_v1")


@pytest.mark.asyncio
async def test_is_processed_returns_false_when_no_store_getter():
    """When _store_getter is None (no production wiring), _is_processed returns False safely."""
    handler, gateway = _make_handler(bot_open_id="ou_bot")
    handler._store_getter = None
    result = await handler._is_processed("evt-2", chat_id="oc_x")
    assert result is False

