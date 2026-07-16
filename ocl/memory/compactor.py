"""MEMORY.md compaction: parsing, serialization, token estimation, expiry, and LLM compression.

Supports both channel-level MEMORY.md and per-agent MEMORY.md.
Per-agent path: channels/<channel_id>/agents/<agent_id>/MEMORY.md
Fallback path:  channels/<channel_id>/MEMORY.md (for default agent / backward compat)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ocl.config import settings
from ocl.llm import curation_acompletion

logger = logging.getLogger(__name__)

# Cache tiktoken encoding at module level (fallback if tiktoken fails)
_encoding = None
try:
    import tiktoken
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning("tiktoken encoding unavailable, using word-count fallback")

_STRUCTURED_RE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\] \[(P[123])\] (.+)$"
)
_LEGACY_RE = re.compile(r"^- (.+)$")


@dataclass
class MemoryEntry:
    date: str
    priority: str
    content: str


def _memory_path(channel_id: str, agent_id: str = "default") -> Path:
    """Return the correct MEMORY.md path for channel/agent."""
    if agent_id and agent_id != "default":
        return settings.channels_dir / channel_id / "agents" / agent_id / "MEMORY.md"
    return settings.channels_dir / channel_id / "MEMORY.md"


def parse_memory(text: str) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    today = date.today().isoformat()

    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue

        m = _STRUCTURED_RE.match(line)
        if m:
            entries.append(MemoryEntry(date=m.group(1), priority=m.group(2), content=m.group(3)))
            continue

        m = _LEGACY_RE.match(line)
        if m:
            entries.append(MemoryEntry(date=today, priority="P2", content=m.group(1)))

    return entries


def serialize(entries: list[MemoryEntry]) -> str:
    if not entries:
        return ""
    lines = ["## Memory", ""]
    for e in entries:
        lines.append(f"- [{e.date}] [{e.priority}] {e.content}")
    lines.append("")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    if _encoding is not None:
        return len(_encoding.encode(text))
    return len(text) // 4


def apply_expiry(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    today = date.today()
    limits = {
        "P1": settings.memory_expiry_days_p1,
        "P2": settings.memory_expiry_days_p2,
        "P3": settings.memory_expiry_days_p3,
    }
    result: list[MemoryEntry] = []
    for e in entries:
        try:
            entry_date = datetime.strptime(e.date, "%Y-%m-%d").date()
        except ValueError:
            result.append(e)
            continue
        age = (today - entry_date).days
        max_age = limits.get(e.priority, limits["P2"])
        if age <= max_age:
            result.append(e)
    return result


async def _compress(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    serialized = serialize(entries)
    system_prompt = (
        "You are a memory compactor. Given a list of memory entries in the format "
        "`- [YYYY-MM-DD] [Pn] text`, merge duplicates and summarize verbose entries. "
        "Output ONLY the compacted entries in the same `- [YYYY-MM-DD] [Pn] text` format. "
        "Never delete P1 entries. Prefer removing or merging P3 entries first, then P2."
    )

    try:
        response = await curation_acompletion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": serialized},
            ],
        )
        output = response.choices[0].message.content or ""
        compressed = parse_memory(output)
        if not compressed:
            logger.warning("compactor: LLM returned empty output, keeping original entries")
            return entries
        return compressed
    except Exception as exc:
        logger.warning("compactor: LLM compression failed (%s), keeping original entries", exc)
        return entries


async def compact_memory(channel_id: str, agent_id: str = "default") -> None:
    """Read, compact (expiry + optional LLM), and write back MEMORY.md."""
    memory_path = _memory_path(channel_id, agent_id)

    if not memory_path.exists():
        return

    raw = memory_path.read_text(encoding="utf-8").strip()
    if not raw:
        return

    entries = parse_memory(raw)
    entries = apply_expiry(entries)

    serialized = serialize(entries)
    token_count = estimate_tokens(serialized)

    threshold = int(settings.memory_max_tokens * settings.memory_compact_threshold)
    if token_count <= threshold:
        memory_path.write_text(serialized, encoding="utf-8")
        return

    entries = await _compress(entries)
    serialized = serialize(entries)

    memory_path.write_text(serialized, encoding="utf-8")
