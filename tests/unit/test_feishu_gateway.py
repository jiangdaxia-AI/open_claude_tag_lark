"""Tests for FeishuGateway — OpenAPI wrapper behavior."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ocl.gateway.feishu.auth import TokenManager
from ocl.gateway.feishu.gateway import FeishuGateway


def _make_gateway() -> FeishuGateway:
    """Build a FeishuGateway with a mocked TokenManager (returns 't-fake')."""
    token_mgr = TokenManager("app", "secret")
    token_mgr._token = "t-fake"
    token_mgr._expires_at = 9_999_999_999.0  # never expires in test
    return FeishuGateway(token_mgr=token_mgr, tenant_id="tenant-001")


@pytest.mark.asyncio
async def test_get_user_name_returns_display_name():
    gw = _make_gateway()

    async def mock_get(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"user": {"name": "Alice Wang", "en_name": "Alice"}},
            },
            request=request,
        )

    with patch.object(httpx.AsyncClient, "get", new=mock_get):
        name = await gw.get_user_name("ou_alice")
    assert name == "Alice Wang"
    await gw.close()


@pytest.mark.asyncio
async def test_get_user_name_falls_back_to_user_id_on_error():
    gw = _make_gateway()

    async def mock_get(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("GET", url)
        return httpx.Response(
            404,
            json={"code": 230002, "msg": "user not found"},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "get", new=mock_get):
        name = await gw.get_user_name("ou_unknown")
    assert name == "ou_unknown"
    await gw.close()


@pytest.mark.asyncio
async def test_send_message_long_text_splits_into_chunks():
    """Text > 30KB is split by paragraphs and sent as multiple messages."""
    gw = _make_gateway()
    # Build 40KB of text with paragraph breaks (double newlines)
    para = "x" * 5000
    long_text = "\n\n".join([para] * 8)  # ~40KB across 8 paragraphs

    call_count = 0

    async def mock_post(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": "om-1"}},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        await gw.send_message(chat_id="oc_test", text=long_text)
    # Expect multiple POST calls because text > 30KB
    assert call_count >= 2
    await gw.close()


@pytest.mark.asyncio
async def test_send_message_returns_message_id():
    gw = _make_gateway()

    async def mock_post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": "om-xyz"}},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        msg_id = await gw.send_message(chat_id="oc_test", text="hello")
    assert msg_id == "om-xyz"
    await gw.close()


@pytest.mark.asyncio
async def test_add_reaction_failure_is_silent():
    """Per spec §6.1 — reaction failures are swallowed with a warning."""
    gw = _make_gateway()

    async def mock_post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            500,
            json={"code": 99999, "msg": "boom"},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        # Should NOT raise
        await gw.add_reaction("om_msg", "thought_balloon")
    await gw.close()


@pytest.mark.asyncio
async def test_get_chat_members_returns_user_id_to_name_mapping():
    gw = _make_gateway()

    async def mock_get(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "member_list": [
                        {"member_id": "ou_a", "name": "Alice"},
                        {"member_id": "ou_b", "name": "Bob"},
                    ],
                    "page_token": "",
                    "has_more": False,
                },
            },
            request=request,
        )

    with patch.object(httpx.AsyncClient, "get", new=mock_get):
        members = await gw.get_chat_members("oc_test")
    assert members == {"ou_a": "Alice", "ou_b": "Bob"}
    await gw.close()


@pytest.mark.asyncio
async def test_send_message_with_special_chars():
    """I5 fix: Messages with quotes and backslashes should be properly encoded."""
    gw = _make_gateway()
    captured_payload = {}

    async def mock_post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        captured_payload.update(kwargs.get('json', {}))
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": "om-test"}},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        await gw.send_message(chat_id="oc_test", text='Hello "world" \\ test')

    # Verify the content is properly JSON-encoded
    import json
    content = json.loads(captured_payload.get("content", "{}"))
    assert content["text"] == 'Hello "world" \\ test'
    await gw.close()


@pytest.mark.asyncio
async def test_long_message_chunks_thread_under_first_message():
    """M6 fix: When a message is split into chunks, subsequent chunks should thread under the first."""
    gw = _make_gateway()
    captured_calls = []

    async def mock_post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        payload = kwargs.get('json', {})
        captured_calls.append(payload)
        request = httpx.Request("POST", url, json=payload)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": f"om-{len(captured_calls)}"}},
            request=request,
        )

    # Build a long message that will be split
    long_text = "\n\n".join(["x" * 10000] * 4)  # Will split into multiple chunks

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        await gw.send_message(chat_id="oc_test", text=long_text)

    # Verify we made multiple calls
    assert len(captured_calls) >= 2

    # First chunk should NOT have reply_in_thread set (unless explicitly provided)
    # (reply_in_thread is only set when reply_to is provided)
    # Subsequent chunks should have reply_in_thread set to True
    # and should reference the first message_id
    first_msg_id = f"om-1"
    for i, call in enumerate(captured_calls):
        if i == 0:
            # First chunk: no reply_in_thread unless explicitly provided
            assert "reply_in_thread" not in call or not call.get("reply_in_thread")
        else:
            # Subsequent chunks: should thread under first message
            assert call.get("reply_in_thread") is True

    await gw.close()
