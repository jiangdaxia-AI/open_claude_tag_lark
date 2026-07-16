"""Feishu docx OpenAPI wrapper + Markdown <-> block conversion.

Markdown conversion is intentionally minimal (5 block types) per spec §4.8:
  - text block (type 2) ↔ plain paragraph
  - heading1/2/3 (types 3/4/5) ↔ `# / ## / ###`
  - bullet (type 12) ↔ `- `
  - code (type 14) ↔ ` ``` `
  - quote (type 15?) not in MVP scope; treated as text

Other block types produce `[unsupported block: <name>]` placeholders.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"

# Feishu docx block_type codes
_BLOCK_TEXT = 2
_BLOCK_HEADING1 = 3
_BLOCK_HEADING2 = 4
_BLOCK_HEADING3 = 5
_BLOCK_BULLET = 12
_BLOCK_CODE = 14

_BLOCK_TYPE_NAMES: dict[int, str] = {
    1: "page",
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    12: "bullet",
    14: "code",
    27: "image",
    # others (table=31, etc.) — surfaced as unsupported placeholder
}


def parse_doc_id(doc_url_or_id: str) -> str:
    """Extract document_id from a Feishu URL, or return the input if already an ID."""
    # URLs look like https://<tenant>.feishu.cn/docx/<id>[/...]  or /wiki/<id>
    match = re.search(r"/(?:docx|wiki)/([A-Za-z0-9]+)", doc_url_or_id)
    if match:
        return match.group(1)
    return doc_url_or_id.strip()


def get_doc_url(doc_id: str) -> str:
    """Build a clickable Feishu doc URL from a document_id."""
    return f"https://feishu.cn/docx/{doc_id}"


# ── Markdown -> Feishu blocks ────────────────────────────────────────────

def markdown_to_blocks(md: str) -> list[dict[str, Any]]:
    """Parse Markdown into a list of Feishu block dicts.

    Minimal scan: line-by-line, recognizing `#`, `##`, `###`, `-`, ` ``` `, and plain text.
    Consecutive non-empty plain lines are merged into a single text block.
    """
    blocks: list[dict[str, Any]] = []
    lines = md.split("\n")

    in_code = False
    code_buffer: list[str] = []

    para_buffer: list[str] = []

    def flush_para():
        if para_buffer:
            text = "\n".join(para_buffer).strip()
            if text:
                blocks.append({
                    "block_type": _BLOCK_TEXT,
                    "text": {"elements": [{"text_run": {"content": text}}]},
                })
            para_buffer.clear()

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                # close code block
                flush_para()
                blocks.append({
                    "block_type": _BLOCK_CODE,
                    "code": {"elements": [{"text_run": {"content": "\n".join(code_buffer)}}]},
                })
                code_buffer.clear()
                in_code = False
            else:
                flush_para()
                in_code = True
            continue
        if in_code:
            code_buffer.append(line)
            continue

        # Heading detection
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading_match:
            flush_para()
            level = len(heading_match.group(1))
            content = heading_match.group(2)
            btype = {1: _BLOCK_HEADING1, 2: _BLOCK_HEADING2, 3: _BLOCK_HEADING3}[level]
            key = f"heading{level}"
            blocks.append({
                "block_type": btype,
                key: {"elements": [{"text_run": {"content": content}}]},
            })
            continue

        # Bullet detection
        bullet_match = re.match(r"^[-*]\s+(.*)$", line)
        if bullet_match:
            flush_para()
            blocks.append({
                "block_type": _BLOCK_BULLET,
                "bullet": {"elements": [{"text_run": {"content": bullet_match.group(1)}}]},
            })
            continue

        # Empty line — flush paragraph
        if not line.strip():
            flush_para()
            continue

        # Plain text — accumulate
        para_buffer.append(line)

    # End of input
    if in_code:
        # unterminated code block — emit as code anyway
        blocks.append({
            "block_type": _BLOCK_CODE,
            "code": {"elements": [{"text_run": {"content": "\n".join(code_buffer)}}]},
        })
    flush_para()
    return blocks


# ── Feishu blocks -> Markdown ────────────────────────────────────────────

def _extract_text_from_elements(elements: list[dict]) -> str:
    parts = []
    for el in elements or []:
        if "text_run" in el:
            parts.append(el["text_run"].get("content", ""))
        elif "mention_user" in el:
            parts.append(f"@{el['mention_user'].get('name', 'user')}")
    return "".join(parts)


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Convert Feishu blocks back to a Markdown string."""
    lines: list[str] = []
    for b in blocks:
        btype = b.get("block_type")
        if btype == _BLOCK_TEXT:
            text = _extract_text_from_elements(b.get("text", {}).get("elements", []))
            lines.append(text)
            lines.append("")
        elif btype == _BLOCK_HEADING1:
            text = _extract_text_from_elements(b.get("heading1", {}).get("elements", []))
            lines.append(f"# {text}")
            lines.append("")
        elif btype == _BLOCK_HEADING2:
            text = _extract_text_from_elements(b.get("heading2", {}).get("elements", []))
            lines.append(f"## {text}")
            lines.append("")
        elif btype == _BLOCK_HEADING3:
            text = _extract_text_from_elements(b.get("heading3", {}).get("elements", []))
            lines.append(f"### {text}")
            lines.append("")
        elif btype == _BLOCK_BULLET:
            text = _extract_text_from_elements(b.get("bullet", {}).get("elements", []))
            lines.append(f"- {text}")
        elif btype == _BLOCK_CODE:
            text = _extract_text_from_elements(b.get("code", {}).get("elements", []))
            lines.append(f"```\n{text}\n```")
            lines.append("")
        else:
            type_name = _BLOCK_TYPE_NAMES.get(btype, f"type_{btype}")
            lines.append(f"[unsupported block: {type_name}]")
            lines.append("")
    return "\n".join(lines).strip()


# ── HTTP API calls ───────────────────────────────────────────────────────

async def create_doc(token: str, title: str) -> str:
    """Create an empty docx. Returns the new document_id."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_BASE}/docx/v1/documents",
            json={"title": title},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"create_doc failed: {data}")
        return data["data"]["document"]["document_id"]


async def write_markdown(token: str, doc_id: str, md: str) -> None:
    """Append markdown content (converted to blocks) to an existing document."""
    blocks = markdown_to_blocks(md)
    if not blocks:
        return
    # Convert blocks to children format for the batch update API
    children = [{"block_type": b["block_type"], **{k: v for k, v in b.items() if k != "block_type"}} for b in blocks]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            json={"index": 0, "children": children},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"write_markdown failed: {data}")


async def read_doc_as_markdown(token: str, doc_url_or_id: str) -> str:
    """Read a doc by URL or ID; return its content as Markdown."""
    doc_id = parse_doc_id(doc_url_or_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_BASE}/docx/v1/documents/{doc_id}/blocks",
            params={"page_size": 500},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"read_doc failed: {data}")
        blocks = data["data"].get("items", [])
        return blocks_to_markdown(blocks)
