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


async def build_system_prompt(
    channel_id: str,
    user_map: dict[str, str],
    agent_config: AgentConfig | None = None,
    mem0_recall: str = "",
    task_text: str = "",
    session_id: str = "",
) -> str:
    """Assemble the system prompt from CHANNEL.md + AGENT.md + memory + skills.

    Three-layer memory:
    - CHANNEL.md is shared across all agents in the channel (team context)
    - AGENT.md + memory are per-agent (individual role and memory)
    - Skills are per-channel (shared)

    When layered memory is enabled, memory is injected via embedding retrieval
    (relevant global + task slices for the current query) instead of the full
    MEMORY.md dump. Falls back to full MEMORY.md when layered memory is
    disabled or has no data yet (cold start).
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

    skills = await _load_skills(channel_id, task_text)

    parts: list[str] = [channel_md.strip()]

    # Inject channel agent roster so the agent knows who to delegate to
    parts.append(_build_agent_roster(channel_id, agent_config.agent_id if agent_config else ""))

    # Language requirement
    parts.append(
        "## 语言要求\n"
        "你必须用中文思考和回复。所有输出（包括推理、工具参数说明、最终回复）都必须是中文。"
    )

    # Inject current task status so agent knows what's done
    parts.append(
        "## 任务状态感知\n"
        "在回复前，用 `task_list(status='done')` 查看已完成的任务，"
        "用 `task_list(status='todo')` 查看待办任务。"
        "已完成的任务不要重复处理。"
        "如果用户问的问题涉及之前的任务，先查任务状态再回复。"
    )

    # Agent-specific role
    if agent_md.strip():
        parts.append(agent_md.strip())

    # Agent-specific memory: layered retrieval (global + task slices relevant
    # to the current query) when enabled; full MEMORY.md as fallback.
    layered_block = ""
    if settings.memory_layered_enabled and task_text:
        from ocl.memory.layered import get_layered_memory

        layered_block = await get_layered_memory().retrieve(
            channel_id,
            agent_config.agent_id if agent_config else "assistant",
            task_text,
            session_id=session_id,
        )

    if layered_block:
        parts.append(
            "## 相关记忆（智能检索）\n"
            "以下是从你的长期记忆中检索到的与当前对话相关的片段：\n\n"
            f"{layered_block}"
        )
    elif memory_md.strip():
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
        "## Capability discovery\n"
        "Before starting a complex task, call `list_capabilities` to see all available tools. "
        "Call `describe_capability(name)` to get detailed usage info for a specific tool.\n"
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
        "\n"
        "## Scheduled task (cron) tools\n"
        "You can register recurring monitoring tasks that wake you on a schedule:\n"
        "- `schedule_task(cron, description)` — register a cron job (5-field cron, e.g. '0 9 * * 1' = every Mon 9am)\n"
        "- `list_crons(status?)` — list all scheduled cron tasks\n"
        "- `cancel_cron(cron_id)` — cancel a scheduled task\n"
        "Use these for periodic checks (stale PRs, deadline reminders, heartbeat monitoring).\n"
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
        "You can delegate tasks to other agents. The system automatically:\n"
        "  1. Sends a visible @mention in the group chat\n"
        "  2. Wakes the target agent to start working\n"
        "  3. Chains sequential tasks — the second task only starts after the first completes\n\n"
        "### How to delegate (use task_create with assignee)\n"
        "When you need another agent to handle something:\n"
        "  task_create(title='...', description='...', assignee='@display_name', priority='P1')\n\n"
        "The assignee should be the agent's @display_name from the roster above.\n"
        "The system handles @mention, waking the agent, and task chaining automatically.\n\n"
        "### When to delegate (IMPORTANT)\n"
        "- Product/PRD/需求文档 tasks → delegate to product agent (@display_name)\n"
        "- Code review/architecture/技术方案 tasks → delegate to code agent (@display_name)\n"
        "- Simple questions, general chat, status checks → answer yourself\n\n"
        "### Coordinator role (if you are the main/default agent)\n"
        "When a user asks you to 'arrange', 'organize', or 'handle' a project that involves\n"
        "multiple roles (e.g. '做个XX小程序，出个需求文档，然后让研发review'):\n"
        "1. Create the first task (e.g. PRD) with task_create(assignee='@产品agent')\n"
        "2. Create the second task (e.g. tech review) with task_create(assignee='@代码agent')\n"
        "   — the system automatically chains it to wait for the first task\n"
        "3. Tell the user the plan — who's doing what, in what order\n"
        "4. Do NOT just check task_list and reply — CREATE the tasks and DELEGATE\n\n"
        "- After all sub-agents finish, post a summary of what was accomplished\n"
        "- Use `task_list` to check progress when asked\n\n"
        "### Orchestration tools (for complex multi-step plans)\n"
        "For tasks with explicit DAG dependencies, use:\n"
        "- `plan_subtasks(tasks)` — create multiple subtasks with depends_on\n"
        "- `run_subtask(task_id)` — start a subtask (self or delegate)\n"
        "- `wait_subtasks(task_ids)` — wait for completion\n"
        "- `get_subtask_status()` — check all subtasks\n\n"
        "### Sandbox code execution tools\n"
        "- `exec_code(code, language?)` — execute code in a sandbox (persists across calls)\n"
        "- `sandbox_read_file(path)` / `sandbox_write_file(path, content)` — file I/O\n"
        "- `sandbox_list_files(path?)` — list files\n"
        "- `sandbox_install_package(package, language?)` — install packages\n"
        "- `list_capabilities()` — see all available tools\n"
    )

    return "\n\n---\n\n".join(parts)


def _build_agent_roster(channel_id: str, current_agent_id: str = "") -> str:
    """Build the agent roster section for the system prompt.

    Lists all available agents in this channel with their display_name and ID,
    so the agent knows exactly who to @mention for delegation.
    """
    from ocl.agents.config import load_agents

    try:
        registry = load_agents(channel_id)
        agents = registry.iter_enabled()
        if not agents:
            return ""
    except Exception:
        return ""

    lines = ["## Channel Agent Roster", ""]
    lines.append("Other agents in this channel (for delegation, use @<display_name> or @<agent_id>):")
    for cfg in agents:
        if cfg.agent_id == current_agent_id:
            continue
        bot_status = "bot ready" if cfg.feishu_bot_open_id else "no bot"
        lines.append(f"  - @{cfg.display_name} (id: {cfg.agent_id}) — {cfg.description} [{bot_status}]")

    if len(lines) <= 3:
        return ""  # No other agents
    return "\n".join(lines)


async def _load_skills(channel_id: str, task_text: str = "") -> list[tuple[str, str]]:
    """Load relevant skills via semantic recall (LLM → keyword → popularity)."""
    from ocl.agent.skills import find_relevant_skills
    return await find_relevant_skills(channel_id, task_text)


async def build_messages(
    channel_id: str,
    user_id: str,
    display_name: str,
    text: str,
    thread_ts: str,
    store: MessageStore,
    agent_id: str = "",
) -> list[dict]:
    """Build the messages list: recent channel history + current message.

    Multi-agent attribution: in a shared channel, several agents post replies.
    Only THIS agent's own past replies are presented as role=assistant (the
    LLM's own outputs). Other agents' replies are presented as role=user with
    a [@display_name] prefix — from this agent's perspective they are
    teammates speaking, not itself. Without this distinction an agent sees
    another agent's words as its own and adopts the wrong identity.
    """
    recent = await store.get_recent_messages(limit=settings.context_window_messages)

    messages: list[dict] = []
    for row in recent:
        if row["role"] == "assistant" and row.get("user_id") == agent_id:
            # My own previous reply — no prefix
            messages.append({"role": "assistant", "content": row["content"]})
        elif row["role"] == "assistant":
            # Another agent's reply — teammate context, not my output
            messages.append({"role": "user", "content": f"[@{row['display_name']}] {row['content']}"})
        else:
            # User messages: prefix with @name for attribution
            messages.append({"role": "user", "content": f"[@{row['display_name']}] {row['content']}"})

    # Append current user message
    messages.append({"role": "user", "content": f"[@{display_name}] {text}"})
    return messages
