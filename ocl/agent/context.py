"""Context assembler — builds the system prompt and message list for the LLM.

Three-layer memory architecture (fused from open-tag):
  ① Channel layer (shared):    CHANNEL.md — team context, all agents read
  ② Agent layer (isolated):    AGENT.md + MEMORY.md — per-agent role and memory
  ③ Task layer (shared):       injected from task_store when relevant
"""

from __future__ import annotations

import logging
from pathlib import Path

from ocl.agents.config import AgentConfig
from ocl.config import settings
from ocl.memory.store import MessageStore

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL_MD = """\
# Channel Agent

You are a helpful AI teammate in this channel.
Be concise, direct, and technical. Ask clarifying questions before taking big actions.
"""

_DEFAULT_AGENT_MD = """\
# Assistant

You are an AI teammate. Be concise, direct, and helpful.
"""


def _read_channel_file(channel_id: str, filename: str, default: str = "") -> str:
    path = settings.channels_dir / channel_id / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return default


def build_system_prompt(
    channel_id: str,
    user_map: dict[str, str],
    agent_config: AgentConfig | None = None,
    mem0_recall: str = "",
) -> str:
    """Assemble the system prompt from CHANNEL.md + AGENT.md + MEMORY.md + skills.

    Three-layer memory:
    - CHANNEL.md is shared across all agents in the channel (team context)
    - AGENT.md + MEMORY.md are per-agent (individual role and memory)
    - Skills are per-channel (shared)
    """
    channel_md = _read_channel_file(channel_id, "CHANNEL.md", _DEFAULT_CHANNEL_MD)

    # Per-agent files (fall back to channel-level MEMORY.md for backward compat)
    if agent_config:
        agent_md = agent_config.read_agent_md()
        memory_md = agent_config.read_memory_md()
        agent_display = agent_config.display_name
    else:
        agent_md = _DEFAULT_AGENT_MD
        memory_md = _read_channel_file(channel_id, "MEMORY.md", "")
        agent_display = "Assistant"

    skills = _load_skills(channel_id)

    parts: list[str] = [channel_md.strip()]

    # Agent-specific role
    if agent_md.strip():
        parts.append(agent_md.strip())

    # Agent-specific memory
    if memory_md.strip():
        parts.append(f"## What I remember as {agent_display}\n\n{memory_md.strip()}")

    # Semantic recall
    if mem0_recall:
        parts.append(f"## 相关记忆 (语义召回)\n\n{mem0_recall}")

    # Skills
    if skills:
        skill_block = "\n\n".join(f"### Skill: {name}\n{content}" for name, content in skills)
        parts.append(f"## Available skills\n\n{skill_block}")

    parts.append(
        "## Multi-user context\n"
        "User messages are prefixed with [@username] for attribution. "
        "Assistant messages have no prefix. "
        "You are a shared teammate — anyone in the channel can see this conversation. "
        "When following up, address the relevant person by @username."
    )

    parts.append(
        "## Memory tools\n"
        "After responding you may call `memory_append`, `memory_replace`, or "
        "`memory_delete` to manage your own long-term memory (MEMORY.md).\n\n"
        "- `memory_append(content, priority)`: Save a new fact. "
        "priority: P1=core facts, P2=preferences (default), P3=transient events.\n"
        "- `memory_replace(old, new)`: Update an outdated fact.\n"
        "- `memory_delete(content)`: Remove an outdated or incorrect entry.\n\n"
        "Only save what future sessions would genuinely benefit from knowing."
    )

    parts.append(
        "## Task tools\n"
        "You can create, claim, assign, update, and list tasks using:\n"
        "- `task_create(title, description?, assignee?, priority?)`\n"
        "- `task_claim(task_id)` — claim for yourself\n"
        "- `task_assign(task_id, assignee)` — hand off to another agent or person\n"
        "- `task_update(task_id, status, description?)` — move: todo→in_progress→in_review→done→closed\n"
        "- `task_list(status?, assignee?)` — see the channel's task board\n"
        "- `task_get(task_id)` — get task details\n\n"
        "When a user asks to 'turn this into a task' or 'track this', use task_create."
    )

    parts.append(
        "## Reminder tools\n"
        "You can schedule reminders for yourself or others:\n"
        "- `reminder_schedule(message, remind_at, target?)` — schedule a reminder\n"
        "- `reminder_list()` — list pending reminders\n"
        "- `reminder_cancel(reminder_id)` — cancel a reminder\n"
    )

    parts.append(
        "## Artifact & workspace tools\n"
        "For long-form outputs (PRDs, reports, code reviews), use `save_artifact` "
        "to save to your workspace and reply with a summary instead of dumping "
        "thousands of words in the chat:\n"
        "- `save_artifact(filename, content, summary)` — save file + return summary\n"
        "\n"
        "## Thread & bookmark tools\n"
        "- `thread_unfollow(thread_id)` — stop watching a thread when your work there is done\n"
        "- `bookmark_message(message_id)` — save a message for later reference\n"
    )

    parts.append(
        "## Agent delegation\n"
        "You can delegate tasks to other agents by mentioning them in your response. "
        "For example: '@DocBot please update the API docs based on this review'. "
        "The other agent will be woken and will pick up the task. "
        "Always confirm with the user before delegating to another agent."
    )

    return "\n\n---\n\n".join(parts)


def _load_skills(channel_id: str) -> list[tuple[str, str]]:
    skills_dir = settings.channels_dir / channel_id / "skills"
    if not skills_dir.exists():
        return []
    results = []
    for path in sorted(skills_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        # Skip archived skills
        if "status: archived" in content:
            continue
        results.append((path.stem, content))
    return results


async def build_messages(
    channel_id: str,
    user_id: str,
    display_name: str,
    text: str,
    thread_ts: str,
    store: MessageStore,
) -> list[dict]:
    """Build the messages list: recent channel history + current message."""
    recent = await store.get_recent_messages(limit=settings.context_window_messages)

    messages: list[dict] = []
    for row in recent:
        role = "assistant" if row["role"] == "assistant" else "user"
        if role == "assistant":
            # Assistant messages: no prefix (avoid timestamp noise accumulating)
            messages.append({"role": role, "content": row["content"]})
        else:
            # User messages: prefix with @name for attribution
            messages.append({"role": role, "content": f"[@{row['display_name']}] {row['content']}"})

    # Append current user message
    messages.append({"role": "user", "content": f"[@{display_name}] {text}"})
    return messages
