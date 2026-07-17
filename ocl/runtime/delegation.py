"""Delegation logic — agent-to-agent task handoff.

Migrated from loop.py to separate the delegation mechanism from the main
agent loop. Two functions:

- maybe_delegate_to_agents: parse @mentions in agent reply, create chained
  tasks, wake the first target agent.
- trigger_downstream: called when a delegated task completes, wakes the next
  agent in the chain.

Both functions retain their original signatures (gateway + scalar params)
for backward compatibility with existing delegation flows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from ocl.agents.config import load_agents

if TYPE_CHECKING:
    from ocl.gateway.base import Gateway

logger = logging.getLogger(__name__)

_MAX_DELEGATION_DEPTH = 5  # prevent infinite agent-to-agent delegation loops


async def maybe_delegate_to_agents(
    gateway: "Gateway",
    channel_id: str,
    text: str,
    current_agent_id: str,
    reply_to: str | None = None,
    session_id: str = "",
    _depth: int = 0,
    _upstream_agents: set[str] | None = None,
) -> None:
    """Parse @<display_name> mentions in text and trigger target agents.

    Single task-creation path for delegation (runtime owns orchestration):
    1. Extract each agent's specific assignment from the text segment
       following their @mention
    2. Create tasks chained by depends_on, tagged with the shared session_id
       (one session = one big task across the whole agent chain)
    3. Send a visible @mention message to the channel
    4. Wake the FIRST target agent with its specific task title/description
    5. trigger_downstream() wakes subsequent agents as tasks complete
    """
    import time as _time

    # Prevent infinite delegation loops (agent A -> B -> A -> B -> ...)
    if _depth >= _MAX_DELEGATION_DEPTH:
        return

    try:
        registry = load_agents(channel_id)
    except Exception:
        return

    # Collect target agents: @<display_name> or @<agent_id> in the text,
    # excluding the current agent (no self-delegation) and agents without bot_open_id.
    # Ordered by first @mention position in text (so "先 @A 再 @B" -> [A, B]).
    targets: list[tuple[str, str, str, int, int]] = []  # (agent_id, display_name, bot_open_id, match_start, match_end)
    seen: set[str] = set()
    # Longest display_name first to avoid prefix collisions during matching
    agents = sorted(registry.iter_enabled(), key=lambda c: len(c.display_name), reverse=True)
    # Build the set of agents to exclude: self + all upstream agents in the chain
    upstream = _upstream_agents or set()
    upstream.add(current_agent_id)

    for cfg in agents:
        if cfg.agent_id in upstream:
            continue
        if not cfg.feishu_bot_open_id:
            continue
        for name in (cfg.display_name, cfg.agent_id):
            if not name:
                continue
            pattern = rf"(^|[\s\W])@{re.escape(name)}(\s+|[，。！!,.:：*]|$)"
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match and cfg.agent_id not in seen:
                targets.append((cfg.agent_id, cfg.display_name, cfg.feishu_bot_open_id, match.start(), match.end()))
                seen.add(cfg.agent_id)
                break

    # Sort targets by their @mention position in text (first mentioned = first executed)
    targets.sort(key=lambda t: t[3])

    if not targets:
        return

    # Check if gateway supports rich-text mentions
    if not hasattr(gateway, "send_message_with_mentions"):
        return

    # Extract each agent's specific assignment: the text between this
    # @mention and the next one (so "@A 做PRD。@B 做评审" gives A="做PRD。")
    def _assignment_for(idx: int) -> str:
        start = targets[idx][4]  # end of this @mention match
        end = targets[idx + 1][3] if idx + 1 < len(targets) else len(text)
        segment = text[start:end].strip(" ，。：:、\n*")
        return segment[:300]

    # Build the mention list and send one message that @mentions all target bots.
    mentions = [{"open_id": bot_open_id, "name": display_name} for _, display_name, bot_open_id, _, _ in targets]
    agent_names = ", ".join(f"@{n}" for _, n, _, _, _ in targets)

    if len(targets) > 1:
        order_hints = " -> ".join(f"@{n}" for _, n, _, _, _ in targets)
        trigger_text = f"任务分配顺序：{order_hints}。请按顺序接手处理。"
    else:
        trigger_text = f"@{targets[0][1]} 上面的任务交给你了，请接手处理。"

    try:
        await gateway.send_message_with_mentions(
            chat_id=channel_id,
            text=trigger_text,
            mentions=mentions,
            reply_to=reply_to,
            agent_id=current_agent_id,
        )
    except Exception:
        logger.exception("Failed to send delegation message to %s", agent_names)

    # Feishu does NOT deliver app_mention events when one bot @mentions another bot.
    # So we wake the target agent directly in-process.
    from ocl.agents.task_store import task_create, task_list, format_task_board
    from ocl.gateway.router import route_message as _route_message

    # Create chained tasks with SPECIFIC titles/descriptions so the woken
    # agent knows exactly what to do (no re-planning, no role confusion).
    task_ids: list[int] = []
    task_infos: list[dict] = []
    prev_task_id = ""
    for idx, (target_agent_id, target_display_name, _oid, _s, _e) in enumerate(targets):
        assignment = _assignment_for(idx)
        # Title: first line / sentence of the assignment, capped short
        title = assignment.split("\n")[0].split("。")[0][:60] or f"@{target_display_name} 接手任务"
        try:
            task = await task_create(
                channel_id=channel_id,
                creator=current_agent_id,
                title=title,
                description=assignment or text[:300],
                assignee=target_agent_id,
                priority="P1",
                depends_on=prev_task_id,
                session_id=session_id,
            )
            tid = task["id"]
            task_ids.append(tid)
            task_infos.append({"id": tid, "title": title, "description": assignment})
            prev_task_id = str(tid)
        except Exception:
            logger.exception("Failed to create delegation task for %s", target_agent_id)

    # Push task board card to channel (active tasks only — no history noise)
    try:
        active_tasks = await task_list(channel_id=channel_id, status="active")
        board_text = format_task_board(active_tasks)
        await gateway.send_message(
            chat_id=channel_id,
            text=board_text,
            agent_id=current_agent_id,
        )
    except Exception:
        logger.exception("Failed to push task board after delegation")

    # Wake only the FIRST target agent (non-blocking, fire-and-forget),
    # with its specific task title and description.
    if targets and task_ids:
        first_agent_id, first_display_name, _, _, _ = targets[0]
        first_info = task_infos[0] if task_infos else {}
        try:
            task_msg = (
                f"我是 @{current_agent_id}。分配给你的任务：\n\n"
                f"**{first_info.get('title', '')}**\n"
                f"{first_info.get('description', '')}\n\n"
                f"这是任务 #{task_ids[0]}，请你自己执行（不要再次分派）。"
                f"完成后调用 task_update(status='done', task_id={task_ids[0]}, result=你的产出摘要)。"
            )
            # Fire-and-forget: don't await, let it run in background
            asyncio.create_task(_route_message(
                gateway=gateway,
                tenant_id=gateway.tenant_id,
                chat_id=channel_id,
                user_id=f"agent:{current_agent_id}",
                text=task_msg,
                message_id=f"delegate_{_time.time()}_0",
                agent_id=first_agent_id,
                _delegation_depth=_depth + 1,
                _upstream_agents=set(upstream),
                _delegation_task_id=task_ids[0],  # pass task ID for downstream triggering
                _session_id=session_id,
            ))
        except Exception:
            logger.exception("Failed to wake first target agent %s", first_agent_id)


async def trigger_downstream(
    gateway: "Gateway",
    channel_id: str,
    completed_task_id: int,
    completed_agent_id: str,
    _depth: int = 0,
    _upstream_agents: set[str] | None = None,
) -> None:
    """Called when an agent finishes its delegated task — trigger downstream agents.

    Event-driven chaining:
    1. Mark the completed task as done
    2. Find tasks that depend on it (status=todo, depends_on contains this task_id)
    3. @mention the next agent and wake it
    4. Push updated task board card
    """
    import time as _time

    from ocl.agents.task_store import find_dependent_tasks, task_get, task_update, task_list, format_task_board
    from ocl.gateway.router import route_message as _route_message

    try:
        # Mark current task as done
        await task_update(channel_id, completed_task_id, status="done")

        # Get the completed task to inherit its session for the chain
        completed_task = await task_get(channel_id, completed_task_id)
        session_id = (completed_task or {}).get("session_id", "")

        # Push updated task board (active tasks only)
        active_tasks = await task_list(channel_id=channel_id, status="active")
        board_text = format_task_board(active_tasks)
        await gateway.send_message(chat_id=channel_id, text=board_text, agent_id=completed_agent_id)

        # Find and trigger downstream tasks
        downstream = await find_dependent_tasks(channel_id, completed_task_id)
        if not downstream:
            # No more downstream tasks — don't auto-wake main agent for summary;
            # delayed replies mix with the user's new messages.
            logger.info("All sub-tasks done (agent=%s) — not auto-waking main agent", completed_agent_id)
            return

        for dtask in downstream:
            next_agent_id = dtask.get("assignee", "")
            if not next_agent_id:
                continue

            # Prevent loops
            upstream = _upstream_agents or set()
            if next_agent_id in upstream:
                continue
            upstream.add(completed_agent_id)

            # Mark downstream task as in_progress
            await task_update(channel_id, dtask["id"], status="in_progress")

            # @mention the next agent in the channel with its specific task
            dtask_title = dtask.get("title", "")
            dtask_desc = dtask.get("description", "")
            try:
                registry = load_agents(channel_id)
                next_cfg = registry.get(next_agent_id)
                if next_cfg and next_cfg.feishu_bot_open_id:
                    await gateway.send_message_with_mentions(
                        chat_id=channel_id,
                        text=f"@{next_cfg.display_name} 前置任务已完成，轮到你了：{dtask_title}",
                        mentions=[{"open_id": next_cfg.feishu_bot_open_id, "name": next_cfg.display_name}],
                        agent_id=completed_agent_id,
                    )
            except Exception:
                logger.exception("Failed to @mention downstream agent %s", next_agent_id)

            # Wake the next agent (non-blocking) with its specific task
            asyncio.create_task(_route_message(
                gateway=gateway,
                tenant_id=gateway.tenant_id,
                chat_id=channel_id,
                user_id=f"agent:{completed_agent_id}",
                text=(
                    f"我是 @{completed_agent_id}。前置任务已完成，现在轮到你执行：\n\n"
                    f"**{dtask_title}**\n{dtask_desc}\n\n"
                    f"这是任务 #{dtask['id']}，请你自己执行（不要再次分派）。"
                    f"完成后调用 task_update(status='done', task_id={dtask['id']}, result=你的产出摘要)。"
                ),
                message_id=f"delegate_{_time.time()}_{dtask['id']}",
                agent_id=next_agent_id,
                _delegation_depth=_depth + 1,
                _upstream_agents=set(upstream),
                _delegation_task_id=dtask["id"],
                _session_id=session_id,
            ))
    except Exception:
        logger.exception("Failed to trigger downstream after task %d", completed_task_id)
