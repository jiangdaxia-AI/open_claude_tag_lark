"""Memory curation turn — inner loop (Letta-inspired).

After the agent replies, it gets one more LLM call to decide what
(if anything) to write to the per-agent MEMORY.md. This keeps memory
clean and agent-curated rather than a noisy append-only log.
"""

from __future__ import annotations

import logging

from ocl.llm import curation_acompletion

logger = logging.getLogger(__name__)

_CURATION_PROMPT = """\
You just responded to a message in a channel. Review the exchange below.

Your job: decide if anything should be persisted to your own long-term memory (MEMORY.md).
Only save facts that future sessions would genuinely benefit from knowing:
- Team conventions or preferences that came up
- Decisions made ("we decided to use X")
- Important context about the team or project
- Corrections to things you got wrong

Priority: P1=core facts (identity, tech stack), P2=preferences (default),
P3=transient events (meetings, reminders).
Use memory_delete to remove outdated entries.

Do NOT save:
- Transient task results (code output, search results)
- Things already in MEMORY.md
- Obvious facts that can be looked up

If something is worth saving, call memory_append or memory_replace.
If nothing is worth saving, do nothing — respond with an empty message.
"""

_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_append",
            "description": "Append a new fact or decision to your MEMORY.md",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact to persist",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["P1", "P2", "P3"],
                        "description": (
                            "P1=core facts, P2=preferences "
                            "(default), P3=transient events"
                        ),
                        "default": "P2",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_replace",
            "description": "Replace an outdated fact in your MEMORY.md with an updated one",
            "parameters": {
                "type": "object",
                "properties": {
                    "old": {"type": "string", "description": "The exact text to replace"},
                    "new": {"type": "string", "description": "The replacement text"},
                },
                "required": ["old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "Delete an outdated or incorrect entry from your MEMORY.md",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The entry text to remove (exact or substring match)",
                    },
                },
                "required": ["content"],
            },
        },
    },
]


async def run_memory_curation(
    channel_id: str,
    system_prompt: str,
    messages: list[dict],
    final_reply: str,
    agent_id: str = "default",
) -> None:
    try:
        response = await curation_acompletion(
            messages=[
                {"role": "system", "content": _CURATION_PROMPT},
                *messages[-6:],
                {"role": "assistant", "content": final_reply},
            ],
            tools=_MEMORY_TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        if not msg.tool_calls:
            return

        import json
        from ocl.agent.loop import _handle_memory_tool

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")
            if fn_name in ("memory_append", "memory_replace", "memory_delete"):
                _handle_memory_tool(channel_id, fn_name, fn_args, agent_id=agent_id)
                logger.info(
                    "Memory updated via curation: %s in channel=%s agent=%s",
                    fn_name, channel_id, agent_id,
                )

    except Exception:
        logger.exception("Memory curation failed for channel=%s agent=%s", channel_id, agent_id)
