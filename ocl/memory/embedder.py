"""Embedding client for layered memory (SiliconFlow / OpenAI-compatible API).

Single responsibility: text -> vector. No caching, no retry magic —
failures propagate to the caller which degrades gracefully (memory
retrieval is best-effort, never blocks the agent loop).
"""

from __future__ import annotations

import logging

import httpx

from ocl.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts. Returns None on failure (caller degrades).

    Uses the OpenAI-compatible /embeddings endpoint. SiliconFlow's
    BAAI/bge-m3 returns 1024-dim vectors.
    """
    if not texts:
        return []
    if not settings.memory_embedder_api_key:
        logger.warning("memory_embedder_api_key not configured — skipping embedding")
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.memory_embedder_api_base}/embeddings",
                headers={"Authorization": f"Bearer {settings.memory_embedder_api_key}"},
                json={"model": settings.memory_embedder_model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            # Sort by index to guarantee order matches input
            items = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in items]
    except Exception:
        logger.exception("Embedding request failed (%d texts)", len(texts))
        return None


async def embed_query(text: str) -> list[float] | None:
    """Embed a single query text. Returns None on failure."""
    vectors = await embed_texts([text])
    if not vectors:
        return None
    return vectors[0]
