"""Tests for feishu_doc_* tool schemas and dispatch."""

from unittest.mock import AsyncMock, patch

import pytest

from ocl.gateway.feishu.auth import TokenManager
from ocl.tools.feishu_docs import FEISHU_DOC_TOOLS, dispatch_feishu_doc


def _make_token_mgr() -> TokenManager:
    mgr = TokenManager("app", "secret")
    mgr._token = "t-fake"
    mgr._expires_at = 9_999_999_999.0
    return mgr


def test_tool_schemas_define_create_and_read():
    names = {t["function"]["name"] for t in FEISHU_DOC_TOOLS}
    assert names == {"feishu_doc_create", "feishu_doc_read"}


@pytest.mark.asyncio
async def test_dispatch_create_returns_url_string():
    token_mgr = _make_token_mgr()
    with patch("ocl.tools.feishu_docs.create_doc", new=AsyncMock(return_value="doc-abc")), \
         patch("ocl.tools.feishu_docs.write_markdown", new=AsyncMock()):
        result = await dispatch_feishu_doc(
            "feishu_doc_create",
            {"title": "Test", "content": "# hello"},
            token_mgr=token_mgr,
        )
    assert "Created document: Test" in result
    assert "feishu.cn/docx/doc-abc" in result


@pytest.mark.asyncio
async def test_dispatch_create_without_content_skips_write():
    token_mgr = _make_token_mgr()
    with patch("ocl.tools.feishu_docs.create_doc", new=AsyncMock(return_value="doc-abc")), \
         patch("ocl.tools.feishu_docs.write_markdown", new=AsyncMock()) as mock_write:
        await dispatch_feishu_doc(
            "feishu_doc_create",
            {"title": "Empty"},
            token_mgr=token_mgr,
        )
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_read_returns_markdown_text():
    token_mgr = _make_token_mgr()
    with patch("ocl.tools.feishu_docs.read_doc_as_markdown", new=AsyncMock(return_value="# Title\nbody")):
        result = await dispatch_feishu_doc(
            "feishu_doc_read",
            {"doc_url_or_id": "https://x.feishu.cn/docx/ABC123"},
            token_mgr=token_mgr,
        )
    assert "# Title" in result


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error_string():
    token_mgr = _make_token_mgr()
    result = await dispatch_feishu_doc("nope", {}, token_mgr=token_mgr)
    assert "Unknown" in result or "not found" in result.lower()


@pytest.mark.asyncio
async def test_dispatch_swallows_exception_and_returns_error_string():
    token_mgr = _make_token_mgr()
    with patch("ocl.tools.feishu_docs.create_doc", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await dispatch_feishu_doc(
            "feishu_doc_create",
            {"title": "x"},
            token_mgr=token_mgr,
        )
    # Should not raise; should return a structured error
    assert "Failed" in result or "error" in result.lower()
