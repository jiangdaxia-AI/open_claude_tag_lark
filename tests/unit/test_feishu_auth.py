"""Tests for Feishu TokenManager — token caching and refresh."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ocl.gateway.feishu.auth import TokenManager


@pytest.mark.asyncio
async def test_token_cached_until_close_to_expiry():
    """Second call within expiry window returns cached token, no new HTTP call."""
    mgr = TokenManager("test_app_id", "test_app_secret")

    call_count = 0

    async def mock_post(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Create a mock request from args[0] (url) and kwargs
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            200,
            json={"tenant_access_token": "t-abc", "expire": 7200},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        t1 = await mgr.get_tenant_token()
        t2 = await mgr.get_tenant_token()
    assert t1 == "t-abc"
    assert t2 == "t-abc"
    assert call_count == 1
    await mgr.close()


@pytest.mark.asyncio
async def test_token_refreshed_when_expired():
    """If expiry has passed, a new HTTP call is made."""
    import time
    mgr = TokenManager("test_app_id", "test_app_secret")

    call_count = 0

    async def mock_post(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        token = "t-1" if call_count == 1 else "t-2"
        # Create a mock request from args[0] (url) and kwargs
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(
            200,
            json={"tenant_access_token": token, "expire": 7200},
            request=request,
        )

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        t1 = await mgr.get_tenant_token()
        # Force expiry
        mgr._expires_at = time.time() - 1
        t2 = await mgr.get_tenant_token()
    assert t1 == "t-1"
    assert t2 == "t-2"
    assert call_count == 2
    await mgr.close()


@pytest.mark.asyncio
async def test_token_refresh_raises_on_http_error():
    """Network/HTTP failures propagate; no silent fallback."""
    mgr = TokenManager("test_app_id", "test_app_secret")

    async def mock_post(self, *args, **kwargs):
        # Create a mock request from args[0] (url) and kwargs
        url = args[0] if args else kwargs.get('url', '')
        request = httpx.Request("POST", url, json=kwargs.get('json', {}))
        return httpx.Response(401, json={"msg": "bad credentials"}, request=request)

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError):
            await mgr.get_tenant_token()
    await mgr.close()
