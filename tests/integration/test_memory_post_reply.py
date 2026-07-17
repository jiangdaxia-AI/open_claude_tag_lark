"""Integration tests for post-reply memory wiring in loop.py."""

from __future__ import annotations

from datetime import date

import pytest

from ocl.runtime.handlers import _handle_memory_tool


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    from ocl import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    channel_dir = tmp_path / "channels" / "C1"
    channel_dir.mkdir(parents=True)
    return channel_dir


def test_memory_append_structured_format(memory_dir):
    """memory_append writes structured format with date and priority."""
    _handle_memory_tool("C1", "memory_append", {"content": "Test fact", "priority": "P1"})
    content = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    today = date.today().isoformat()
    assert f"- [{today}] [P1] Test fact" in content


def test_memory_append_default_priority(memory_dir):
    """memory_append defaults to P2 when priority not specified."""
    _handle_memory_tool("C1", "memory_append", {"content": "Another fact"})
    content = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "[P2]" in content


def test_memory_delete_removes_matching_line(memory_dir):
    """memory_delete removes lines containing the target text."""
    mem_file = memory_dir / "MEMORY.md"
    mem_file.write_text(
        "- [2026-07-01] [P2] Keep this\n"
        "- [2026-07-01] [P3] Delete this entry\n"
        "- [2026-07-01] [P1] Also keep\n",
        encoding="utf-8",
    )
    _handle_memory_tool("C1", "memory_delete", {"content": "Delete this entry"})
    content = mem_file.read_text(encoding="utf-8")
    assert "Delete this entry" not in content
    assert "Keep this" in content
    assert "Also keep" in content


def test_memory_replace_still_works(memory_dir):
    """memory_replace continues to work with raw string replacement."""
    mem_file = memory_dir / "MEMORY.md"
    mem_file.write_text("- old fact\n", encoding="utf-8")
    _handle_memory_tool("C1", "memory_replace", {"old": "old fact", "new": "new fact"})
    content = mem_file.read_text(encoding="utf-8")
    assert "new fact" in content
    assert "old fact" not in content
