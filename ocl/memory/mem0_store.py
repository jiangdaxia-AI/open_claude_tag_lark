"""Mem0 semantic memory store — singleton wrapper with async helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ocl.config import settings

if TYPE_CHECKING:
    from mem0 import Memory

logger = logging.getLogger(__name__)

_mem0_instance: "Memory | None" = None
_mem0_initialised = False


def _build_config() -> dict:
    """Build Mem0 config dict from application settings."""
    vector_store_path = settings.mem0_vector_store_path or str(settings.data_dir / "mem0_vectors")
    return {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "ocl",
                "path": vector_store_path,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": settings.mem0_embedder_model,
                "api_base": settings.mem0_embedder_api_base,
                "api_key": settings.mem0_embedder_api_key,
            },
        } if settings.mem0_embedder_api_base else {
            "provider": "huggingface",
            "config": {
                "model": settings.mem0_embedder_model,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": settings.mem0_llm_model,
                "api_base": settings.mem0_llm_api_base,
                "api_key": settings.mem0_llm_api_key,
                "temperature": 0.1,
                "max_tokens": 1000,
            },
        },
    }


def get_mem0() -> "Memory | None":
    """Return the global Mem0 singleton, or None if mem0 is disabled."""
    global _mem0_instance, _mem0_initialised  # noqa: PLW0603

    if not settings.mem0_enabled:
        return None

    if not _mem0_initialised:
        _mem0_initialised = True
        try:
            from mem0 import Memory  # noqa: PLC0415

            _mem0_instance = Memory.from_config(_build_config())
        except Exception:
            logger.exception("Failed to initialise Mem0 — semantic recall disabled")
            _mem0_instance = None

    return _mem0_instance


async def mem0_add(channel_id: str, user_id: str, user_text: str, final_reply: str) -> None:
    """Store a conversation turn in Mem0. Failures are swallowed."""
    mem0 = get_mem0()
    if mem0 is None:
        return

    mem0_messages = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": final_reply},
    ]
    try:
        await asyncio.to_thread(
            mem0.add,
            messages=mem0_messages,
            user_id=user_id,
            agent_id=channel_id,
            metadata={"channel_id": channel_id},
        )
    except Exception:
        logger.exception("mem0_add failed for user=%s channel=%s", user_id, channel_id)


async def mem0_search(channel_id: str, user_id: str, query: str) -> str:
    """Search Mem0 for relevant memories. Returns formatted string or empty string."""
    mem0 = get_mem0()
    if mem0 is None:
        return ""

    try:
        results = await asyncio.to_thread(
            mem0.search,
            query=query,
            top_k=settings.mem0_search_top_k,
            user_id=user_id,
            agent_id=channel_id,
        )
        if results:
            return "\n".join(f"- {r['memory']}" for r in results)
        return ""
    except Exception:
        logger.exception("mem0_search failed for user=%s channel=%s", user_id, channel_id)
        return ""
