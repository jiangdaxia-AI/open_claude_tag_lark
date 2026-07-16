"""Feishu text utilities — clean <at> tags from message content.

Feishu embeds mentions as `<at user_id="ou_x">Name</at>` in raw message text.
These helpers convert them to plain `@Name` for cleaner agent context.
"""

from __future__ import annotations

import re

_AT_PATTERN = re.compile(r'<at user_id="([^"]+)">(.*?)</at>')


def clean_at_tags(text: str, user_map: dict[str, str] | None = None) -> str:
    """Convert `<at user_id="ou_x">Name</at>` → `@Name`.

    If `user_map` is provided, override the display name with the mapped value.
    """
    def replace(m: re.Match[str]) -> str:
        uid = m.group(1)
        name = m.group(2)
        if user_map and uid in user_map:
            name = user_map[uid]
        return f"@{name}"
    return _AT_PATTERN.sub(replace, text)


def extract_mentioned_user_ids(text: str) -> list[str]:
    """Return all `user_id` values mentioned in `<at>` tags, in order."""
    return [m.group(1) for m in _AT_PATTERN.finditer(text)]
