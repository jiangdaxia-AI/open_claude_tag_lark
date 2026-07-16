"""Tests for ocl.memory.compactor."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ocl.memory.compactor import (
    MemoryEntry,
    apply_expiry,
    compact_memory,
    estimate_tokens,
    parse_memory,
    serialize,
)


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_structured_memory():
    text = (
        "## Channel Memory\n\n"
        "- [2026-01-01] [P1] Critical fact\n"
        "- [2026-02-15] [P2] Medium fact\n"
        "- [2026-03-10] [P3] Low-priority note\n"
    )
    entries = parse_memory(text)
    assert len(entries) == 3
    assert entries[0] == MemoryEntry("2026-01-01", "P1", "Critical fact")
    assert entries[1] == MemoryEntry("2026-02-15", "P2", "Medium fact")
    assert entries[2] == MemoryEntry("2026-03-10", "P3", "Low-priority note")


def test_parse_unstructured_legacy():
    text = "- some fact without prefix\n"
    entries = parse_memory(text)
    assert len(entries) == 1
    assert entries[0].priority == "P2"
    assert entries[0].date == today()
    assert entries[0].content == "some fact without prefix"


def test_parse_mixed():
    text = (
        "## Channel Memory\n\n"
        "- [2026-01-01] [P1] Structured entry\n"
        "- legacy line without date\n"
    )
    entries = parse_memory(text)
    assert len(entries) == 2
    assert entries[0].priority == "P1"
    assert entries[1].priority == "P2"
    assert entries[1].date == today()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_serialize_roundtrip():
    original = [
        MemoryEntry("2026-01-01", "P1", "Alpha"),
        MemoryEntry("2026-02-01", "P2", "Beta"),
        MemoryEntry("2026-03-01", "P3", "Gamma"),
    ]
    text = serialize(original)
    assert text.startswith("## Channel Memory")
    parsed = parse_memory(text)
    assert parsed == original


def test_serialize_empty():
    assert serialize([]) == ""


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def test_estimate_tokens():
    text = "Hello, this is a test string for token counting."
    count = estimate_tokens(text)
    assert count > 0
    assert count < len(text) * 2


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_apply_expiry_p3_14days():
    """P3 entry 15 days old should be removed (default limit = 14)."""
    entries = [MemoryEntry(_days_ago(15), "P3", "Old P3 fact")]
    result = apply_expiry(entries)
    assert result == []


def test_apply_expiry_p3_within_limit():
    """P3 entry 10 days old should be kept (default limit = 14)."""
    entries = [MemoryEntry(_days_ago(10), "P3", "Recent P3 fact")]
    result = apply_expiry(entries)
    assert len(result) == 1


def test_apply_expiry_p1_not_expired():
    """P1 entry 300 days old should be kept (default limit = 365)."""
    entries = [MemoryEntry(_days_ago(300), "P1", "Important P1 fact")]
    result = apply_expiry(entries)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# compact_memory integration
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_channel(tmp_path):
    """Create a temp channel directory with a MEMORY.md file."""
    channel_id = "test_ch_001"
    channel_dir = tmp_path / channel_id
    channel_dir.mkdir()
    return tmp_path, channel_id, channel_dir


async def test_compact_memory_under_budget_no_compress(tmp_channel, monkeypatch):
    """When token count is under budget, no LLM call should be made."""
    tmp_path, channel_id, channel_dir = tmp_channel

    memory_file = channel_dir / "MEMORY.md"
    memory_file.write_text(
        "## Channel Memory\n\n- [2026-01-01] [P1] Short fact\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "ocl.memory.compactor.settings",
        MagicMock(
            channels_dir=tmp_path,
            memory_max_tokens=2000,
            memory_compact_threshold=0.8,
            memory_expiry_days_p1=365,
            memory_expiry_days_p2=60,
            memory_expiry_days_p3=14,
        ),
    )

    mock_curation = AsyncMock()
    with patch("ocl.memory.compactor.curation_acompletion", mock_curation):
        await compact_memory(channel_id)

    mock_curation.assert_not_called()
    assert memory_file.exists()


@pytest.mark.asyncio
async def test_compact_memory_over_budget_calls_llm(tmp_channel, monkeypatch):
    """When token count exceeds budget threshold, curation_acompletion should be called."""
    tmp_path, channel_id, channel_dir = tmp_channel

    # Build a large memory file
    lines = ["## Channel Memory", ""]
    for i in range(200):
        lines.append(
            f"- [{today()}] [P2] This is a fairly long memory entry number {i} "
            "with some verbose text to pad tokens"
        )
    memory_file = channel_dir / "MEMORY.md"
    memory_file.write_text("\n".join(lines), encoding="utf-8")

    compressed_response = MagicMock()
    compressed_response.choices[0].message.content = (
        "- [2026-01-01] [P2] Compressed summary of all entries\n"
    )

    mock_curation = AsyncMock(return_value=compressed_response)

    monkeypatch.setattr(
        "ocl.memory.compactor.settings",
        MagicMock(
            channels_dir=tmp_path,
            memory_max_tokens=100,
            memory_compact_threshold=0.8,
            memory_expiry_days_p1=365,
            memory_expiry_days_p2=60,
            memory_expiry_days_p3=14,
        ),
    )

    with patch("ocl.memory.compactor.curation_acompletion", mock_curation):
        await compact_memory(channel_id)

    mock_curation.assert_called_once()
