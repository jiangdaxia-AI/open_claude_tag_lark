"""Feishu cloud-doc tools — LiteLLM-compatible schemas + dispatcher.

Two tools:
- feishu_doc_create(title, content?) -> URL string
- feishu_doc_read(doc_url_or_id)     -> Markdown string

Both rely on a TokenManager (injected by the agent loop via dispatch_tool).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ocl.gateway.feishu.docs_api import (
    create_doc,
    get_doc_url,
    write_markdown,
    read_doc_as_markdown,
)

if TYPE_CHECKING:
    from ocl.gateway.feishu.auth import TokenManager

logger = logging.getLogger(__name__)


FEISHU_DOC_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_create",
            "description": "Create a new Feishu document with title and optional Markdown content. Returns the document URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "content": {
                        "type": "string",
                        "description": "Initial content in Markdown (optional)",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_read",
            "description": "Read the content of a Feishu document by URL or document_id. Returns Markdown text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_url_or_id": {
                        "type": "string",
                        "description": "Feishu doc URL or document_id",
                    },
                },
                "required": ["doc_url_or_id"],
            },
        },
    },
]


async def dispatch_feishu_doc(
    fn_name: str,
    args: dict[str, Any],
    token_mgr: "TokenManager",
) -> str:
    """Run a feishu_doc_* tool. Returns a string for the agent to consume."""
    try:
        token = await token_mgr.get_tenant_token()

        if fn_name == "feishu_doc_create":
            title = args.get("title", "Untitled")
            content = args.get("content")
            doc_id = await create_doc(token=token, title=title)
            if content:
                await write_markdown(token=token, doc_id=doc_id, md=content)
            url = get_doc_url(doc_id)
            logger.info("feishu_doc_create: %s -> %s", title, url)
            return f"Created document: {title}\nURL: {url}"

        if fn_name == "feishu_doc_read":
            doc_url_or_id = args.get("doc_url_or_id", "")
            md = await read_doc_as_markdown(token=token, doc_url_or_id=doc_url_or_id)
            logger.info("feishu_doc_read: %s (%d chars)", doc_url_or_id, len(md))
            return md

        return f"Unknown feishu doc tool: {fn_name}"

    except Exception as e:
        logger.warning("feishu_doc tool %s failed: %s", fn_name, e)
        return f"Failed to {fn_name}: {type(e).__name__}: {e}"
