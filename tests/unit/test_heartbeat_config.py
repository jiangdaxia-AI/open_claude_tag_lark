"""Tests for HEARTBEAT.md frontmatter parsing."""

from ocl.ambient.config import parse_heartbeat_md, HeartbeatConfig


def test_parse_full_config():
    text = """\
---
enabled: true
cron: "0 9 * * 1"
max_recent_messages: 15
---

# Focus areas
- surface unanswered questions
"""
    cfg = parse_heartbeat_md(text)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.cron == "0 9 * * 1"
    assert cfg.max_recent_messages == 15
    assert "surface unanswered questions" in cfg.guidance


def test_parse_defaults_max_recent_to_30():
    text = "---\nenabled: true\ncron: \"0 * * * *\"\n---\nbody\n"
    cfg = parse_heartbeat_md(text)
    assert cfg.max_recent_messages == 30


def test_parse_disabled():
    text = "---\nenabled: false\ncron: \"0 * * * *\"\n---\nbody\n"
    cfg = parse_heartbeat_md(text)
    assert cfg.enabled is False


def test_parse_returns_none_when_no_frontmatter():
    cfg = parse_heartbeat_md("just some markdown, no frontmatter")
    assert cfg is None


def test_parse_returns_none_when_missing_required_field():
    # cron is required
    text = "---\nenabled: true\n---\nbody\n"
    cfg = parse_heartbeat_md(text)
    assert cfg is None
