"""Integration tests for Mem0 recall injection into system prompt."""

from __future__ import annotations

import pytest

from ocl.agent.context import build_system_prompt


@pytest.fixture()
def channel_dir(tmp_path, monkeypatch):
    from ocl import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    ch_dir = tmp_path / "channels" / "C1"
    ch_dir.mkdir(parents=True)
    return ch_dir


def test_mem0_recall_injected_into_system_prompt(channel_dir):
    """Mem0 search results injected when non-empty."""
    recall = "- User prefers dark mode\n- Project uses FastAPI"
    prompt = build_system_prompt("C1", {}, mem0_recall=recall)
    assert "## 相关记忆 (语义召回)" in prompt
    assert "User prefers dark mode" in prompt
    assert "Project uses FastAPI" in prompt


def test_mem0_recall_empty_not_injected(channel_dir):
    """Empty Mem0 results don't add section to system prompt."""
    prompt = build_system_prompt("C1", {}, mem0_recall="")
    assert "相关记忆" not in prompt


def test_mem0_recall_default_not_injected(channel_dir):
    """Default (no mem0_recall arg) doesn't inject section."""
    prompt = build_system_prompt("C1", {})
    assert "相关记忆" not in prompt
