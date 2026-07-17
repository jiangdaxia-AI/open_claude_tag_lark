"""Context manager — compress long conversation histories to fit context window.

When the message list exceeds a threshold, older messages are summarized
by an LLM into a compact summary. This allows long-running tasks (e.g.
research, code writing) to continue without hitting context limits.

Strategy:
  - If messages <= threshold: return as-is (no compression)
  - If messages > threshold: summarize the oldest N/2 messages into a
    single system message, keep the recent N/2 messages verbatim
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocl.config import settings

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)

# Threshold: compress when messages exceed this count
_COMPRESS_THRESHOLD = 40  # roughly 20 rounds of conversation


class ContextManager:
    """Manages context window by compressing old messages."""

    def __init__(self, threshold: int = _COMPRESS_THRESHOLD) -> None:
        self.threshold = threshold

    async def maybe_compress(
        self,
        rt: "AgentRuntime",
        messages: list[dict],
    ) -> list[dict]:
        """Compress messages if they exceed the threshold.

        Returns the (possibly compressed) message list.
        If no compression needed, returns the original list unchanged.
        """
        if len(messages) <= self.threshold:
            return messages

        # Split: keep recent half, summarize older half
        keep_count = self.threshold // 2
        old_messages = messages[:-keep_count]
        recent_messages = messages[-keep_count:]

        # Build a summary of old messages
        summary = await self._summarize(rt, old_messages)

        # Replace old messages with a summary system message
        summary_msg = {
            "role": "system",
            "content": (
                f"## Previous conversation summary\n\n"
                f"The following is a summary of earlier conversation rounds "
                f"({len(old_messages)} messages compressed):\n\n{summary}"
            ),
        }

        logger.info(
            "Context compressed: %d messages -> 1 summary + %d recent (%s/%s)",
            len(old_messages), len(recent_messages), rt.channel_id, rt.agent_id,
        )

        return [summary_msg] + recent_messages

    async def _summarize(
        self,
        rt: "AgentRuntime",
        messages: list[dict],
    ) -> str:
        """Use LLM to summarize a list of messages into a compact summary."""
        # Build a text representation of messages for summarization
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multimodal content — extract text parts
                content = " ".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            lines.append(f"[{role}] {content}")

        conversation_text = "\n".join(lines)

        prompt = (
            "Summarize the following conversation history concisely. "
            "Focus on: key decisions, results obtained, tasks completed, "
            "and any important context that would be needed to continue the work. "
            "Keep it under 500 words.\n\n"
            f"Conversation:\n{conversation_text[:8000]}"
        )

        try:
            from ocl.llm import acompletion

            response = await acompletion(
                channel_id=rt.channel_id,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.choices[0].message.content or ""
            return summary.strip()
        except Exception as e:
            logger.warning("Context summarization failed: %s, using truncated history", e)
            # Fallback: return a truncated version of the conversation
            return f"(Summary unavailable, last {len(messages)} messages truncated)\n" + conversation_text[-2000:]


# ── Global singleton ─────────────────────────────────────────────────────────

_global_context_manager: ContextManager | None = None


def get_context_manager() -> ContextManager:
    """Get or create the global ContextManager instance."""
    global _global_context_manager
    if _global_context_manager is None:
        _global_context_manager = ContextManager()
    return _global_context_manager
