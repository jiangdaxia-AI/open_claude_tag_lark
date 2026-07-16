"""Parse ``HEARTBEAT.md`` frontmatter (enabled / cron / max_recent_messages)
plus the markdown body used as heartbeat guidance.

Hand-written minimal frontmatter parser — only flat scalar keys
(bool / int / quoted-or-unquoted string). No nested YAML. This avoids a
PyYAML dependency (see spec §7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ocl.config import settings

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatConfig:
    enabled: bool
    cron: str
    max_recent_messages: int
    guidance: str


def parse_heartbeat_md(text: str) -> HeartbeatConfig | None:
    """Parse HEARTBEAT.md content. Returns None on missing/invalid frontmatter."""
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        return None

    enabled_raw = frontmatter.get("enabled")
    cron = frontmatter.get("cron")
    if enabled_raw is None or cron is None:
        logger.warning("HEARTBEAT.md missing required 'enabled' or 'cron'")
        return None

    enabled = _parse_bool(enabled_raw)
    max_recent = _parse_int(frontmatter.get("max_recent_messages"), default=30)
    cron_clean = _strip_quotes(str(cron))
    return HeartbeatConfig(
        enabled=enabled,
        cron=cron_clean,
        max_recent_messages=max_recent,
        guidance=body.strip(),
    )


def load_channel_heartbeat_config(channel_id: str) -> HeartbeatConfig | None:
    """Read channels/<channel_id>/HEARTBEAT.md. Returns None if file missing."""
    path: Path = settings.channels_dir / channel_id / "HEARTBEAT.md"
    if not path.exists():
        return None
    try:
        return parse_heartbeat_md(path.read_text())
    except Exception:
        logger.exception("Failed to parse HEARTBEAT.md for channel=%s", channel_id)
        return None


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split ``---\nkey: val\n---\n<body>``. Returns (None, text) if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    fm: dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "---":
            body = "\n".join(lines[idx + 1:])
            return fm, body
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
        idx += 1
    return None, text  # no closing ---


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


def _parse_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _strip_quotes(value: str) -> str:
    """Remove surrounding quotes from a string value."""
    value = value.strip()
    if len(value) >= 2:
        if (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'"):
            return value[1:-1]
    return value
