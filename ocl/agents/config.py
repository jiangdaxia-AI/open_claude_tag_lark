"""Per-agent configuration: load agents from global template or per-channel override.

Architecture — ZERO manual per-channel setup:
  1. Global template (data/templates/agents.toml) — define ALL agents ONCE
  2. Auto-initialization — first message in ANY new chat copies the template
  3. Per-channel override — optional: if data/channels/<chat_id>/agents.toml
     exists, it takes priority over the global template

Directory layout (per channel — auto-created on first message):
  channels/<channel_id>/
    CHANNEL.md                          ← shared channel context (all agents read)
    agents.toml                         ← multi-agent config (bot credentials, roles)
    agents/
      <agent_id>/
        AGENT.md                        ← agent role/persona
        MEMORY.md                       ← agent-specific long-term memory (per-channel)
        tools.toml                      ← agent-specific tool config (optional)

For backward compatibility:
  - If data/templates/agents.toml doesn't exist, falls back to per-channel agents.toml
  - If neither exists, falls back to single "default" agent using global FEISHU_* credentials
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import toml

from ocl.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for one agent in a channel."""

    agent_id: str
    display_name: str
    description: str = ""
    # Feishu bot credentials for this specific agent (multi-bot mode, Q1:B)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bot_open_id: str = ""
    # Is this the default/fallback agent for un-mentioned messages? (Q2:A)
    is_default: bool = False
    # Idle timeout in seconds before this agent goes to sleep
    idle_timeout_seconds: int = 600  # 10 min default
    # Agent scope permissions — list of allowed scopes.
    # Empty list = all scopes granted (backward compat). Non-empty = restrict.
    # Available scopes: message:send, task:create, task:claim, task:assign,
    #   task:update, reminder:schedule, memory:write, delegation:send,
    #   web:search, python:run, mcp:*, file:upload
    scopes: list[str] = None  # type: ignore[assignment]

    def has_scope(self, scope: str) -> bool:
        """Check if agent has a given scope. Empty scopes list = all granted."""
        if not self.scopes:
            return True
        # Check exact match or wildcard (e.g. "mcp:*" matches "mcp:github")
        for s in self.scopes:
            if s == scope or s.endswith("*") and scope.startswith(s[:-1]):
                return True
        return False
    # Parent channel ID — set during load for correct per-channel file paths
    channel_id: str = ""

    @property
    def agent_dir(self) -> Path:
        """Directory for this agent's config files (under the channel dir)."""
        if not self.channel_id:
            raise ValueError(f"AgentConfig.channel_id is not set for agent={self.agent_id}")
        return settings.channels_dir / self.channel_id / "agents" / self.agent_id

    def agent_file(self, filename: str) -> Path:
        return self.agent_dir / filename

    def read_agent_md(self) -> str:
        path = self.agent_file("AGENT.md")
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"# {self.display_name}\n\nYou are {self.display_name}. {self.description}"

    def read_memory_md(self) -> str:
        path = self.agent_file("MEMORY.md")
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def read_tools_toml(self) -> dict[str, Any]:
        path = self.agent_file("tools.toml")
        if path.exists():
            try:
                return toml.loads(path.read_text())
            except Exception:
                logger.warning("Failed to parse tools.toml for agent %s", self.agent_id)
        return {}

    @property
    def workspace_dir(self) -> Path:
        """Agent's private workspace directory for file artifacts."""
        return self.agent_dir / "workspace"

    def ensure_workspace(self) -> Path:
        """Create workspace dir if needed, return the path."""
        ws = self.workspace_dir
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def ensure_dir(self) -> None:
        """Create agent directory and seed default files."""
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        agent_md = self.agent_file("AGENT.md")
        if not agent_md.exists():
            # Try to copy from global template persona first
            template_agent_md = settings.templates_dir / "agents" / self.agent_id / "AGENT.md"
            if template_agent_md.exists():
                agent_md.write_text(template_agent_md.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                agent_md.write_text(
                    f"# {self.display_name}\n\n"
                    f"You are {self.display_name}, an AI teammate in this channel.\n"
                    f"You respond when mentioned by @{self.display_name}.\n"
                    f"Be concise, direct, and helpful.\n",
                    encoding="utf-8",
                )
        memory_md = self.agent_file("MEMORY.md")
        if not memory_md.exists():
            memory_md.write_text("## Agent Memory\n\n", encoding="utf-8")


# ── channel-level registry ──


class ChannelAgentRegistry:
    """Holds all agents for a single channel, with lookup helpers."""

    def __init__(self, channel_id: str, agents: list[AgentConfig]) -> None:
        self.channel_id = channel_id
        self._agents: dict[str, AgentConfig] = {}
        self._default: AgentConfig | None = None
        for a in agents:
            a.channel_id = channel_id
            self._agents[a.agent_id] = a
            if a.is_default:
                self._default = a
        if self._default is None and agents:
            self._default = agents[0]
            self._default.is_default = True

    def get(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(agent_id)

    def get_default(self) -> AgentConfig | None:
        return self._default

    def get_by_bot_open_id(self, bot_open_id: str) -> AgentConfig | None:
        """Find an agent by its Feishu bot_open_id (for multi-bot @mention routing)."""
        for cfg in self._agents.values():
            if cfg.feishu_bot_open_id and cfg.feishu_bot_open_id == bot_open_id:
                return cfg
        return None

    def get_by_display_name(self, name: str) -> AgentConfig | None:
        """Find an agent by display name (case-insensitive)."""
        name_lower = name.lower()
        for cfg in self._agents.values():
            if cfg.display_name.lower() == name_lower or cfg.agent_id.lower() == name_lower:
                return cfg
        return None

    def iter_enabled(self):
        return (cfg for cfg in self._agents.values())

    def all_agent_names(self) -> list[str]:
        """Return all agent display names (for @mention parsing)."""
        return [cfg.display_name for cfg in self._agents.values()]

    def ensure_dirs(self) -> None:
        for cfg in self._agents.values():
            cfg.ensure_dir()


# ── global cache ──

_registry_cache: dict[str, ChannelAgentRegistry] = {}


def _load_toml_safe(path: Path) -> dict[str, Any]:
    """Load a TOML file and return its parsed content. Logs and returns {} on error."""
    try:
        return toml.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed to parse %s: %s", path, exc)
        return {}


def _build_agents_from_toml(
    data: dict[str, Any],
    channel_id: str,
) -> list[AgentConfig]:
    """Build AgentConfig objects from parsed TOML with channel_id bound."""
    agents: list[AgentConfig] = []
    default_seen = False
    for entry in data.get("agent", []):
        is_default = entry.get("is_default", False)
        if is_default:
            default_seen = True
        agent = AgentConfig(
            agent_id=entry["id"],
            display_name=entry.get("display_name", entry["id"]),
            description=entry.get("description", ""),
            feishu_app_id=entry.get("feishu_app_id", settings.feishu_app_id),
            feishu_app_secret=entry.get("feishu_app_secret", settings.feishu_app_secret),
            feishu_bot_open_id=entry.get("feishu_bot_open_id", ""),
            is_default=is_default,
            idle_timeout_seconds=entry.get("idle_timeout_seconds", 600),
            scopes=entry.get("scopes", None),
            channel_id=channel_id,
        )
        agents.append(agent)
    # If no explicit default, mark the first one as default
    if agents and not default_seen:
        agents[0].is_default = True
    return agents


def _auto_init_channel_from_template(channel_id: str) -> Path | None:
    """Initialize a NEW channel directory by copying the global template.

    Only runs if:
      1. The channel directory doesn't already exist, AND
      2. The global template at data/templates/agents.toml exists.

    Returns the agents.toml path, or None if no template or already initialized.
    """
    template_dir = settings.templates_dir
    template_toml = template_dir / "agents.toml"

    if not template_toml.exists():
        logger.debug("No global template at %s", template_toml)
        return None

    channel_dir = settings.channels_dir / channel_id
    if channel_dir.exists():
        logger.debug("Channel dir already exists: %s", channel_dir)
        return channel_dir / "agents.toml"

    logger.info(
        "New channel %s — auto-initializing from template %s",
        channel_id,
        template_dir,
    )

    try:
        shutil.copytree(template_dir, channel_dir)
    except Exception as exc:
        logger.exception("Failed to copy template to %s: %s", channel_dir, exc)
        return None

    agents_toml = channel_dir / "agents.toml"
    if agents_toml.exists():
        agent_count = len(_load_toml_safe(agents_toml).get("agent", []))
        logger.info("Channel %s initialized with %d agents", channel_id, agent_count)
    return agents_toml


def load_agents(channel_id: str) -> ChannelAgentRegistry:
    """Load agents for a channel. Auto-initializes from global template if needed.

    Resolution order (highest to lowest):
      1. Cache hit → return immediately
      2. Per-channel agents.toml (data/channels/<id>/agents.toml) → load it
      3. Global template (data/templates/agents.toml) → auto-init channel from it
      4. Fallback: single "default" agent using global Feishu credentials
    """
    if channel_id in _registry_cache:
        return _registry_cache[channel_id]

    channel_agents_toml = settings.channels_dir / channel_id / "agents.toml"

    # Auto-initialize new channels from the global template
    if not channel_agents_toml.exists():
        initialized = _auto_init_channel_from_template(channel_id)
        if initialized:
            channel_agents_toml = initialized

    if not channel_agents_toml.exists():
        # Fallback: single "default" agent using global Feishu credentials
        registry = ChannelAgentRegistry(
            channel_id=channel_id,
            agents=[
                AgentConfig(
                    agent_id="default",
                    display_name="Assistant",
                    feishu_app_id=settings.feishu_app_id,
                    feishu_app_secret=settings.feishu_app_secret,
                    feishu_bot_open_id=settings.feishu_bot_open_id,
                    is_default=True,
                    channel_id=channel_id,
                )
            ],
        )
        _registry_cache[channel_id] = registry
        return registry

    data = _load_toml_safe(channel_agents_toml)
    agents = _build_agents_from_toml(data, channel_id)
    if not agents:
        logger.warning("No agents defined in %s; using fallback default agent", channel_agents_toml)
        agents = [
            AgentConfig(
                agent_id="default",
                display_name="Assistant",
                feishu_app_id=settings.feishu_app_id,
                feishu_app_secret=settings.feishu_app_secret,
                feishu_bot_open_id=settings.feishu_bot_open_id,
                is_default=True,
                channel_id=channel_id,
            )
        ]

    registry = ChannelAgentRegistry(channel_id=channel_id, agents=agents)
    registry.ensure_dirs()
    _registry_cache[channel_id] = registry
    return registry


def get_all_initialized_channel_ids() -> list[str]:
    """Return all channel IDs that already have on-disk config (for startup pre-warm)."""
    if not settings.channels_dir.exists():
        return []
    return [
        d.name for d in settings.channels_dir.iterdir()
        if d.is_dir() and (d / "agents.toml").exists()
    ]


def clear_cache() -> None:
    """Clear the registry cache (useful for tests)."""
    _registry_cache.clear()


def resolve_agent_from_text(channel_id: str, text: str) -> tuple[str, str]:
    """Parse @Agent mention from message text and return (agent_id, cleaned_text).

    Returns (default_agent_id, original_text) if no explicit @Agent is found.

    Matching is case-insensitive on display_name and agent_id, and longest-match-first
    to avoid prefix collisions (e.g. "CodeBot" matches before "Bot").
    """
    registry = load_agents(channel_id)
    default_id = registry.get_default().agent_id if registry.get_default() else "default"

    agents = sorted(registry.iter_enabled(), key=lambda c: len(c.display_name), reverse=True)

    for cfg in agents:
        for name in (cfg.display_name, cfg.agent_id):
            if not name:
                continue
            pattern = rf"(^|\s)@{re.escape(name)}(\s+|$)"
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                cleaned = text[: match.start()] + text[match.end() :]
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                return cfg.agent_id, cleaned

    return default_id, text
