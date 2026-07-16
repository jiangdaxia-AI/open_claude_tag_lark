"""Gateway protocol — platform-agnostic interface for chat platforms.

Agent loop depends only on this interface. Each platform (Feishu, Discord, etc.)
implements it. This isolates platform-specific APIs (lark-oapi) from
the agent core.
"""

from __future__ import annotations

from typing import Protocol


class Gateway(Protocol):
    """Platform-agnostic chat gateway contract.

    Agent loop only uses these methods; it has zero knowledge of which
    platform it's running on.
    """

    @property
    def tenant_id(self) -> str:
        """Current workspace/tenant ID."""
        ...

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a text message to a chat. Returns the new message_id.

        - chat_id works for both group chats and p2p chats.
        - reply_to, if set, requests the platform to thread the reply.
        - agent_id, if set, sends the message as the bot associated with that agent
          (multi-bot mode). Falls back to the primary bot if not registered.
        - Long text is split into chunks by the implementation.
        """
        ...

    async def send_message_with_mentions(
        self,
        chat_id: str,
        text: str,
        mentions: list[dict],
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a message with rich-text @mentions. Returns the new message_id.

        - mentions: list of {"open_id": "...", "name": "..."} for each user/bot to @.
        - The implementation renders platform-native mention syntax (e.g. Feishu
          post format with <at> tags) so the platform triggers real mention events.
        - text may contain @Name placeholders that get replaced with actual mentions.
        """
        ...

    async def get_user_name(self, user_id: str) -> str:
        """Resolve a user_id to a display name. Falls back to user_id on failure."""
        ...

    async def get_chat_members(self, chat_id: str) -> dict[str, str]:
        """Return a {user_id: name} mapping for all members of a chat."""
        ...

    async def add_reaction(self, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to a message. Failures are logged and swallowed."""
        ...

    async def update_message(
        self,
        message_id: str,
        text: str,
        agent_id: str | None = None,
    ) -> None:
        """Update an existing message's content (for streaming output).

        Failures are logged and swallowed — streaming is best-effort.
        """
        ...

    async def send_card_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a card message (for streaming). Returns message_id."""
        ...

    async def update_card_message(
        self,
        message_id: str,
        text: str,
        agent_id: str | None = None,
    ) -> None:
        """Update a card message's content (instant render in Feishu client)."""
        ...

    async def remove_reaction(self, message_id: str, emoji: str) -> None:
        """Remove an emoji reaction. Failures are logged and swallowed."""
        ...

    async def upload_file(
        self,
        chat_id: str,
        file_path: str,
        file_name: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Upload a file to a chat. Returns the message_id of the file message."""
        ...
