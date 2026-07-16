"""Tests for Feishu docs API wrapper and Markdown conversion."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ocl.gateway.feishu import docs_api


# ── Markdown → blocks conversion ─────────────────────────────────────────

def test_markdown_to_blocks_heading():
    md = "# Title\n\nSome paragraph."
    blocks = docs_api.markdown_to_blocks(md)
    types = [b["block_type"] for b in blocks]
    assert 3 in types  # heading1 block type code (we use 3 for H1, 4 for H2, 5 for H3)
    assert any("Some paragraph" in str(b) for b in blocks)


def test_markdown_to_blocks_bullet_list():
    md = "- one\n- two\n- three"
    blocks = docs_api.markdown_to_blocks(md)
    # Each bullet becomes a block; we should see 3 list blocks
    list_blocks = [b for b in blocks if b["block_type"] == 12]  # 12 = bullet
    assert len(list_blocks) == 3


def test_markdown_to_blocks_code_block():
    md = "```\nprint('hello')\n```"
    blocks = docs_api.markdown_to_blocks(md)
    code_blocks = [b for b in blocks if b["block_type"] == 14]  # 14 = code
    assert len(code_blocks) == 1


def test_markdown_to_blocks_plain_paragraph():
    md = "Just a plain paragraph."
    blocks = docs_api.markdown_to_blocks(md)
    # Block type 2 = text block in Feishu docx schema
    text_blocks = [b for b in blocks if b["block_type"] == 2]
    assert len(text_blocks) == 1


# ── blocks → Markdown conversion ─────────────────────────────────────────

def test_blocks_to_markdown_heading():
    blocks = [
        {"block_type": 3, "heading1": {"elements": [{"text_run": {"content": "Title"}}]}},
    ]
    md = docs_api.blocks_to_markdown(blocks)
    assert md.startswith("# Title")


def test_blocks_to_markdown_unsupported_block_yields_placeholder():
    blocks = [
        {"block_type": 27, "image": {"token": "img_v3_001"}},  # image
        {"block_type": 2, "text": {"elements": [{"text_run": {"content": "After"}}]}},
    ]
    md = docs_api.blocks_to_markdown(blocks)
    assert "[unsupported block: image]" in md
    assert "After" in md


# ── URL parsing ──────────────────────────────────────────────────────────

def test_parse_doc_id_from_url():
    url = "https://example.feishu.cn/docx/ABC12345?from=from_copy"
    assert docs_api.parse_doc_id(url) == "ABC12345"


def test_parse_doc_id_from_plain_id():
    assert docs_api.parse_doc_id("ABC12345") == "ABC12345"


# ── create_doc / read_doc (HTTP mocked) ──────────────────────────────────

@pytest.mark.asyncio
async def test_create_doc_returns_document_id():
    fake_request = httpx.Request("POST", "https://open.feishu.cn/open-apis/docx/v1/documents")
    fake_resp = httpx.Response(
        200,
        json={"code": 0, "data": {"document": {"document_id": "doc-abc"}}},
        request=fake_request
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=fake_resp)):
        doc_id = await docs_api.create_doc(token="t-fake", title="My Doc")
    assert doc_id == "doc-abc"


# ── Roundtrip tests ────────────────────────────────────────────────────────

def test_roundtrip_simple_document():
    """Test that a simple document survives MD -> blocks -> MD roundtrip."""
    original_md = """# Title

This is a paragraph.

- Item 1
- Item 2

## Subtitle

More text.
"""
    blocks = docs_api.markdown_to_blocks(original_md)
    result_md = docs_api.blocks_to_markdown(blocks)
    # The roundtrip should preserve the main content (whitespace may vary)
    assert "# Title" in result_md
    assert "This is a paragraph" in result_md
    assert "- Item 1" in result_md
    assert "- Item 2" in result_md
    assert "## Subtitle" in result_md
    assert "More text" in result_md


def test_roundtrip_code_block():
    """Test that code blocks survive the roundtrip."""
    original_md = """```python
def hello():
    print('world')
```"""
    blocks = docs_api.markdown_to_blocks(original_md)
    result_md = docs_api.blocks_to_markdown(blocks)
    assert "def hello():" in result_md
    assert "print('world')" in result_md


def test_markdown_to_blocks_multi_paragraph():
    """Test that multiple paragraphs are handled correctly."""
    md = """First paragraph.

Second paragraph.

Third paragraph."""
    blocks = docs_api.markdown_to_blocks(md)
    # Each paragraph should be a separate block (they're separated by blank lines)
    text_blocks = [b for b in blocks if b["block_type"] == 2]
    assert len(text_blocks) == 3
