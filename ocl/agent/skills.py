"""Skill auto-creation + semantic recall (Hermes-inspired).

Two responsibilities:
  1. maybe_create_skill() — after complex tasks, write SKILL.md
  2. find_relevant_skills() — before agent loop, decide which skills to load

Skill recall strategy (cascading fallback):
  ① LLM judge: give the LLM all skill names+descriptions + current task,
     ask "which are relevant?" → returns top-K skill names
  ② Keyword fallback: if LLM fails, match task text against skill
     description/tags/keywords (frontmatter or body)
  ③ Popularity fallback: if no keyword match, load top-K by `uses` count
  ④ No load: if no skills at all, return empty (avoid context pollution)
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from ocl.config import settings
from ocl.llm import acompletion

logger = logging.getLogger(__name__)

_MAX_SKILLS_IN_CONTEXT = 3  # don't pollute context with too many skills

_SKILL_CREATION_PROMPT = """\
The agent just completed a complex multi-step task. Review the conversation below and decide
whether it reveals a reusable procedure worth saving as a skill.

A skill is worth saving if:
- The task involved a non-obvious sequence of steps
- The same task is likely to come up again in this channel
- The agent made useful discoveries (gotchas, correct tool order, edge cases)

If yes, write a SKILL.md with this exact format:

---
name: <short-kebab-slug>
description: <one sentence — what task this skill handles>
created: {today}
tool_calls_in_session: {tool_calls}
uses: 0
last_used: null
status: active
---

## When to use this
<1-2 sentences>

## Steps
<numbered list>

## Known gotchas
<bullet list, or "None">

If no skill is worth saving, respond with exactly: SKIP
"""


async def maybe_create_skill(
    channel_id: str,
    messages: list[dict],
    final_reply: str,
    tool_call_count: int,
) -> None:
    skills_dir = settings.channels_dir / channel_id / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    conversation_summary = _summarize_messages(messages[-20:])  # last 20 to keep prompt tight
    prompt = _SKILL_CREATION_PROMPT.format(today=date.today(), tool_calls=tool_call_count)

    try:
        response = await acompletion(
            channel_id=channel_id,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Conversation:\n{conversation_summary}\n\nFinal reply:\n{final_reply}"},
            ],
        )
        content = response.choices[0].message.content or ""
        if content.strip() == "SKIP":
            return

        # Extract the name from the frontmatter
        name = "unnamed"
        for line in content.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
                break

        skill_path = skills_dir / f"{name}.md"
        # Don't overwrite existing skills
        if skill_path.exists():
            skill_path = skills_dir / f"{name}-{int(__import__('time').time())}.md"

        skill_path.write_text(content)
        logger.info("Skill created: %s in channel=%s", skill_path.name, channel_id)

    except Exception:
        logger.exception("Skill creation failed for channel=%s", channel_id)


def _summarize_messages(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if role in ("user", "assistant") and content:
            lines.append(f"{role.upper()}: {content[:300]}")
    return "\n".join(lines)


# ── Skill recall (semantic match before agent loop) ─────────────────────────


def _parse_skill_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter from a SKILL.md file."""
    fm: dict[str, str] = {}
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return fm
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm


def _list_skills(channel_id: str) -> list[tuple[str, Path, dict[str, str], str]]:
    """Return all active skills as (name, path, frontmatter, full_content)."""
    skills_dir = settings.channels_dir / channel_id / "skills"
    if not skills_dir.exists():
        return []
    results = []
    for path in sorted(skills_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        if "status: archived" in content:
            continue
        fm = _parse_skill_frontmatter(content)
        results.append((path.stem, path, fm, content))
    return results


_SKILL_RECALL_PROMPT = """\
You are deciding which skills are relevant to a new task.

Available skills:
{skill_list}

New task: {task}

Return ONLY the skill names that are directly relevant to this task, one per line.
If none are relevant, respond with exactly: NONE
Maximum {max_k} skills.
"""


async def find_relevant_skills(
    channel_id: str, task_text: str
) -> list[tuple[str, str]]:
    """Decide which skills to load into context for this task.

    Cascading fallback:
    ① LLM judge (if skills <= 15 and LLM available)
    ② Keyword match on frontmatter description/tags + body
    ③ Popularity: top-K by `uses` count
    ④ Empty if nothing matches
    """
    all_skills = _list_skills(channel_id)
    if not all_skills:
        return []

    # If only a few skills, just load them all (no need for LLM judge)
    if len(all_skills) <= _MAX_SKILLS_IN_CONTEXT:
        return [(name, content) for name, _, _, content in all_skills]

    # ① LLM judge
    try:
        skill_list_str = "\n".join(
            f"- {name}: {fm.get('description', '(no description)')}"
            for name, _, fm, _ in all_skills
        )
        prompt = _SKILL_RECALL_PROMPT.format(
            skill_list=skill_list_str, task=task_text[:500], max_k=_MAX_SKILLS_IN_CONTEXT
        )
        response = await acompletion(
            channel_id=channel_id,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw and raw.upper() != "NONE":
            # Parse skill names from LLM response
            llm_names = set()
            for line in raw.splitlines():
                name = line.strip().lstrip("-").strip()
                if name:
                    llm_names.add(name)
            # Match against actual skill names
            matched = [
                (name, content)
                for name, _, _, content in all_skills
                if name in llm_names
            ]
            if matched:
                logger.debug(
                    "Skill recall (LLM): %s for channel=%s",
                    [n for n, _ in matched], channel_id
                )
                return matched[:_MAX_SKILLS_IN_CONTEXT]
    except Exception:
        logger.debug("LLM skill recall failed, falling back to keyword match")

    # ② Keyword fallback
    task_lower = task_text.lower()
    keyword_matched = []
    for name, _, fm, content in all_skills:
        # Check frontmatter description + tags, plus body keywords
        searchable = " ".join([
            fm.get("description", ""),
            fm.get("tags", ""),
            fm.get("keywords", ""),
            name,
        ]).lower()
        # Also check if any significant word from task appears in skill body
        if any(word in searchable or word in content.lower()[:500]
               for word in task_lower.split() if len(word) > 2):
            keyword_matched.append((name, content))
    if keyword_matched:
        logger.debug(
            "Skill recall (keyword): %s for channel=%s",
            [n for n, _ in keyword_matched], channel_id
        )
        return keyword_matched[:_MAX_SKILLS_IN_CONTEXT]

    # ③ Popularity fallback: top-K by `uses` count
    def _uses(item: tuple[str, Path, dict[str, str], str]) -> int:
        try:
            return int(item[2].get("uses", "0"))
        except (ValueError, TypeError):
            return 0

    popular = sorted(all_skills, key=_uses, reverse=True)[:_MAX_SKILLS_IN_CONTEXT]
    logger.debug(
        "Skill recall (popularity): %s for channel=%s",
        [n for n, _, _, _ in popular], channel_id
    )
    return [(name, content) for name, _, _, content in popular]
