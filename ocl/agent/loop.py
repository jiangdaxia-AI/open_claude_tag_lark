"""Agent loop — ReAct-style: think → tool call → observe → repeat → reply → memory curation.

Supports multi-agent mode: each invocation carries an agent_id that determines
which agent's AGENT.md, MEMORY.md, and tool set to use.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ocl.agent.context import build_messages, build_system_prompt
from ocl.agent.skills import maybe_create_skill
from ocl.agents.config import AgentConfig, load_agents
from ocl.agents.lifecycle import wake_agent
from ocl.agents.task_store import dispatch_task_tool, TASK_TOOL_SCHEMAS
from ocl.agents.reminder_store import dispatch_reminder_tool, REMINDER_TOOL_SCHEMAS
from ocl.config import settings
from ocl.llm import acompletion, acompletion_stream, vision_acompletion
from ocl.memory.compactor import compact_memory
from ocl.memory.mem0_store import mem0_add, mem0_search
from ocl.memory.store import MessageStore, get_store
from ocl.memory.writer import run_memory_curation
from ocl.tools import registry as _tool_registry
from ocl.tools.registry import get_channel_tools, dispatch_tool

if TYPE_CHECKING:
    from ocl.gateway.base import Gateway

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10  # prevent runaway loops


async def run_agent_loop(
    gateway: "Gateway",
    workspace_id: str,
    channel_id: str,
    user_id: str,
    display_name: str,
    text: str,
    message_id: str,
    store: MessageStore,
    agent_id: str = "default",
    image_base64: str | None = None,
    _delegation_depth: int = 0,
    _upstream_agents: set[str] | None = None,
) -> None:
    # Create cancellation token and ledger entry for this run
    from ocl.agents.cancel import create_cancel_token, remove_cancel_token, is_cancelled
    from ocl.agents.ledger import LedgerEntry, create_entry as ledger_create, finalize_entry as ledger_finalize

    cancel_token = create_cancel_token(channel_id, agent_id)
    ledger_entry = LedgerEntry(
        channel_id=channel_id,
        agent_id=agent_id,
        trigger_user_id=user_id,
        trigger_message=text[:500],
        delegation_depth=_delegation_depth,
        upstream_chain=list(_upstream_agents or []),
    )
    try:
        await ledger_create(ledger_entry)
    except Exception:
        pass

    # Wake the agent (record activity, reset idle timer)
    wake_agent(channel_id, agent_id)

    # Load agent config for per-agent AGENT.md / MEMORY.md
    agent_config: AgentConfig | None = None
    try:
        registry = load_agents(channel_id)
        agent_config = registry.get(agent_id)
    except Exception:
        logger.warning("Failed to load agent config for channel=%s agent=%s", channel_id, agent_id)

    # Persist the incoming user message
    await store.add_message(
        ts=message_id,
        role="user",
        user_id=user_id,
        display_name=display_name,
        content=text,
        thread_ts=message_id,
    )

    # Fetch user map for multi-user attribution
    try:
        user_map = await gateway.get_chat_members(chat_id=channel_id)
    except Exception:
        user_map = {user_id: display_name}

    # Warm the MCP tool cache
    if _tool_registry._mcp_mgr is not None:
        await _tool_registry._mcp_mgr.warm_tools(channel_id)

    mem0_recall = await mem0_search(channel_id, user_id, text)
    system_prompt = build_system_prompt(channel_id, user_map, agent_config=agent_config, mem0_recall=mem0_recall)
    messages = await build_messages(channel_id, user_id, display_name, text, message_id, store)

    if image_base64:
        _attach_image_to_last_message(messages, image_base64)

    tools = get_channel_tools(channel_id)
    # Add task + reminder tools to every agent's toolset
    tools = list(tools) if tools else []
    tools.extend(TASK_TOOL_SCHEMAS)
    tools.extend(REMINDER_TOOL_SCHEMAS)

    tool_call_count = 0
    final_text = ""
    streamed_message_id: str | None = None

    for _round in range(MAX_TOOL_ROUNDS):
        # Check for cancellation between rounds
        if is_cancelled(cancel_token):
            final_text = "任务已取消。"
            if streamed_message_id:
                await gateway.update_card_message(
                    message_id=streamed_message_id,
                    text=final_text,
                    agent_id=agent_id,
                )
            break
        # Use streaming only on the final (text-only) round — when there are no
        # tools, or when tools are present but the model chooses to reply with text.
        # For tool-call rounds, use non-streaming (we need the full tool_calls list).
        use_stream = True  # always stream; if tool_calls arrive, we collect them from the stream

        _llm = vision_acompletion if image_base64 else acompletion
        if use_stream and not image_base64:
            streamed_text = ""
            streamed_reasoning = ""
            tool_calls_chunks: list[dict] = []
            last_patch_time = 0.0
            import time as _time_mod
            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"[{_time_mod.time()}] STREAMING branch entered, round={_round}\n")

            async for chunk in acompletion_stream(
                channel_id=channel_id,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=tools or None,
                tool_choice="auto" if tools else None,
            ):
                delta = chunk.choices[0].delta

                # Accumulate reasoning content (qwen3.5 etc. — "thinking" phase)
                # Show it live so the user sees the agent is working, not hung.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    if not streamed_reasoning:
                        streamed_reasoning = "💭 思考中...\n\n"
                    streamed_reasoning += reasoning

                    # If no final text yet, show reasoning as the streaming content
                    if not streamed_text:
                        display_text = streamed_reasoning
                    else:
                        display_text = streamed_reasoning + "\n\n---\n\n" + streamed_text

                    now = _time_mod.time()
                    if now - last_patch_time > 0.3 and display_text.strip():
                        if streamed_message_id is None:
                            streamed_message_id = await gateway.send_card_message(
                                chat_id=channel_id,
                                text=display_text,
                                reply_to=message_id,
                                agent_id=agent_id,
                            )
                            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                                _f.write(f"  STREAM send_card (reasoning) id={streamed_message_id} len={len(display_text)}\n")
                        else:
                            await gateway.update_card_message(
                                message_id=streamed_message_id,
                                text=display_text,
                                agent_id=agent_id,
                            )
                        last_patch_time = now

                # Normal text content streaming
                if delta.content:
                    streamed_text += delta.content

                    # Throttle PATCH updates to every 0.3s for smooth streaming
                    now = _time_mod.time()
                    if now - last_patch_time > 0.3 and streamed_text.strip():
                        if streamed_message_id is None:
                            # Send card message on first chunk — cards update instantly
                            streamed_message_id = await gateway.send_card_message(
                                chat_id=channel_id,
                                text=streamed_text,
                                reply_to=message_id,
                                agent_id=agent_id,
                            )
                            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                                _f.write(f"  STREAM send_card id={streamed_message_id} text_len={len(streamed_text)}\n")
                        else:
                            await gateway.update_card_message(
                                message_id=streamed_message_id,
                                text=streamed_text,
                                agent_id=agent_id,
                            )
                        last_patch_time = now

                # Accumulate tool calls from stream
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        while len(tool_calls_chunks) <= idx:
                            tool_calls_chunks.append({"id": "", "function": {"name": "", "arguments": ""}})
                        if tc.id:
                            tool_calls_chunks[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_chunks[idx]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_chunks[idx]["function"]["arguments"] += tc.function.arguments

            # Final PATCH: if we have final text, show only that (drop reasoning).
            # If only reasoning (no final text + has tool calls), clear the card
            # since the agent is about to execute tools (next round will stream fresh).
            if streamed_message_id:
                if streamed_text:
                    await gateway.update_card_message(
                        message_id=streamed_message_id,
                        text=streamed_text,
                        agent_id=agent_id,
                    )
                    with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                        _f.write(f"  STREAM final update_card text_len={len(streamed_text)}\n")
                elif tool_calls_chunks and streamed_reasoning:
                    # Only reasoning came through, tools are about to execute —
                    # update card to show the tool action instead of raw reasoning
                    tool_names = [tc["function"]["name"] for tc in tool_calls_chunks if tc["function"]["name"]]
                    if tool_names:
                        await gateway.update_card_message(
                            message_id=streamed_message_id,
                            text=f"⚙️ 执行工具: {', '.join(tool_names)}...",
                            agent_id=agent_id,
                        )

            final_text = streamed_text

            # If tool calls were streamed, execute them (non-streaming tool round)
            if tool_calls_chunks:
                final_text = ""  # not final yet, will continue loop
                # Reconstruct assistant message with tool_calls for the messages list
                messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                        for tc in tool_calls_chunks
                    ],
                })
                for tc in tool_calls_chunks:
                    tool_call_count += 1
                    fn_name = tc["function"]["name"]
                    fn_args = json.loads(tc["function"]["arguments"] or "{}")

                    logger.info("Tool call: %s(%s) in channel=%s agent=%s", fn_name, fn_args, channel_id, agent_id)

                    if fn_name.startswith("task_"):
                        result = await dispatch_task_tool(fn_name, fn_args, channel_id=channel_id, agent_id=agent_id)
                    elif fn_name.startswith("reminder_"):
                        result = await dispatch_reminder_tool(fn_name, fn_args, channel_id=channel_id, agent_id=agent_id)
                    elif fn_name in ("memory_append", "memory_replace", "memory_delete"):
                        _handle_memory_tool(channel_id, fn_name, fn_args, agent_id=agent_id)
                        result = "Memory updated."
                    else:
                        result = await dispatch_tool(fn_name, fn_args, channel_id=channel_id, store=store, agent_id=agent_id, user_id=user_id)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })
                # Keep streamed_message_id so next round UPDATES the same card
                # (instead of sending a new one). Only clear the text buffers.
                # Note: card already updated to "⚙️ 执行工具" above in the final PATCH block.
                streamed_text = ""
                streamed_reasoning = ""
                # Do NOT reset streamed_message_id — reuse the same card next round
                continue  # next round

            break  # pure text response — we're done

        else:
            # Non-streaming fallback (vision or non-stream path)
            response = await _llm(
                channel_id=channel_id,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=tools or None,
                tool_choice="auto" if tools else None,
            )

            choice = response.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                final_text = msg.content or getattr(msg, "reasoning_content", "") or ""
                break

            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                tool_call_count += 1
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or "{}")

                logger.info("Tool call: %s(%s) in channel=%s agent=%s", fn_name, fn_args, channel_id, agent_id)

                if fn_name.startswith("task_"):
                    result = await dispatch_task_tool(fn_name, fn_args, channel_id=channel_id, agent_id=agent_id)
                elif fn_name.startswith("reminder_"):
                    result = await dispatch_reminder_tool(fn_name, fn_args, channel_id=channel_id, agent_id=agent_id)
                elif fn_name in ("memory_append", "memory_replace", "memory_delete"):
                    _handle_memory_tool(channel_id, fn_name, fn_args, agent_id=agent_id)
                    result = "Memory updated."
                else:
                    result = await dispatch_tool(fn_name, fn_args, channel_id=channel_id, store=store, agent_id=agent_id, user_id=user_id)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })
    else:
        final_text = "I reached my tool call limit. Here's what I found so far."

    if not final_text:
        final_text = "Done."

    # If we streamed the final text, the message is already sent — don't send again.
    # Only send here if we didn't stream (e.g. tool-call limit reached, or empty stream).
    if streamed_message_id is None:
        await gateway.send_message(
            chat_id=channel_id,
            text=final_text,
            reply_to=message_id,
            agent_id=agent_id,
        )
    elif final_text == "Done.":
        # Edge case: LLM produced no final text (only reasoning/tools).
        # Update the card to show "Done." instead of leaving it on reasoning/tool status.
        await gateway.update_card_message(
            message_id=streamed_message_id,
            text=final_text,
            agent_id=agent_id,
        )

    # Agent delegation: parse @<display_name> in the reply and trigger target agents.
    # Sends a follow-up message with real Feishu <at> mentions so the target bot
    # receives an app_mention event and its agent wakes up to handle the task.
    await _maybe_delegate_to_agents(
        gateway=gateway,
        channel_id=channel_id,
        text=final_text,
        current_agent_id=agent_id,
        reply_to=message_id,
        _depth=_delegation_depth,
        _upstream_agents=_upstream_agents,
    )

    # Persist assistant reply
    await store.add_message(
        ts=str(__import__("time").time()),
        role="assistant",
        user_id=agent_id,
        display_name=agent_config.display_name if agent_config else "agent",
        content=final_text,
        thread_ts=message_id,
    )

    # Inner loop: memory curation turn (Letta-inspired)
    await run_memory_curation(channel_id, system_prompt, messages, final_text, agent_id=agent_id)
    await compact_memory(channel_id, agent_id=agent_id)
    await mem0_add(channel_id, user_id, text, final_text)

    # Skill auto-creation if task was complex
    if tool_call_count >= 5:
        await maybe_create_skill(channel_id, messages, final_text, tool_call_count)

    # Finalize ledger entry
    ledger_entry.set_output(final_text, streamed_message_id or "", streamed=bool(streamed_message_id))
    if is_cancelled(cancel_token):
        ledger_entry.set_cancelled()
    try:
        await ledger_finalize(ledger_entry)
    except Exception:
        pass

    # Cleanup cancel token
    remove_cancel_token(channel_id, agent_id, cancel_token)


def _handle_memory_tool(
    channel_id: str, fn_name: str, args: dict[str, Any], agent_id: str = "default"
) -> None:
    from datetime import date

    # Per-agent memory: channels/<channel_id>/agents/<agent_id>/MEMORY.md
    if agent_id != "default":
        memory_path = settings.channels_dir / channel_id / "agents" / agent_id / "MEMORY.md"
    else:
        memory_path = settings.channels_dir / channel_id / "MEMORY.md"

    memory_path.parent.mkdir(parents=True, exist_ok=True)
    current = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    if fn_name == "memory_append":
        entry = args.get("content", "").strip()
        priority = args.get("priority", "P2")
        if priority not in ("P1", "P2", "P3"):
            priority = "P2"
        if entry:
            today = date.today().isoformat()
            line = f"- [{today}] [{priority}] {entry}"
            memory_path.write_text(
                current.rstrip() + f"\n{line}\n", encoding="utf-8"
            )

    elif fn_name == "memory_replace":
        old = args.get("old", "")
        new = args.get("new", "")
        memory_path.write_text(current.replace(old, new), encoding="utf-8")

    elif fn_name == "memory_delete":
        target = args.get("content", "").strip()
        if target:
            lines = current.splitlines(keepends=True)
            filtered = [ln for ln in lines if target not in ln]
            memory_path.write_text("".join(filtered), encoding="utf-8")


def _attach_image_to_last_message(messages: list[dict], image_base64: str) -> None:
    """Replace the last message's string content with multimodal content blocks."""
    if not messages:
        return
    last = messages[-1]
    text_content = last.get("content", "")
    last["content"] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
        {"type": "text", "text": text_content},
    ]


_DELEGATION_DEPTH_KEY = "_delegation_depth"
_MAX_DELEGATION_DEPTH = 5  # prevent infinite agent-to-agent delegation loops


async def _maybe_delegate_to_agents(
    gateway: "Gateway",
    channel_id: str,
    text: str,
    current_agent_id: str,
    reply_to: str | None = None,
    _depth: int = 0,
    _upstream_agents: set[str] | None = None,
) -> None:
    """Parse @<display_name> mentions in text and trigger target agents.

    For each @mention that matches another agent's display_name (and that agent
    has a bot_open_id), send a follow-up rich-text message with a real Feishu
    <at> tag. Feishu will then deliver an app_mention event to that bot, which
    wakes the target agent's loop via the normal event handler.

    This is the "agent delegation" mechanism — one agent can hand off work to
    another by @mentioning it in its reply.

    Targets are executed sequentially (not in parallel) so that downstream
    agents can see upstream agents' output in the channel history.
    """
    import re

    # File-based debug log
    import time as _time
    with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
        _f.write(f"[{_time.time()}] _maybe_delegate_to_agents: depth={_depth} current={current_agent_id} text={text[:120]!r}\n")

    # Prevent infinite delegation loops (agent A → B → A → B → ...)
    if _depth >= _MAX_DELEGATION_DEPTH:
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"  MAX DELEGATION DEPTH ({_MAX_DELEGATION_DEPTH}) reached — stopping\n")
        return

    try:
        registry = load_agents(channel_id)
    except Exception as _e:
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"[{_time.time()}] load_agents FAILED: {_e!r}\n")
        return

    # Collect target agents: @<display_name> or @<agent_id> in the text,
    # excluding the current agent (no self-delegation) and agents without bot_open_id.
    # Ordered by first @mention position in text (so "先 @A 再 @B" → [A, B]).
    targets: list[tuple[str, str, str, int]] = []  # (agent_id, display_name, bot_open_id, pos_in_text)
    seen: set[str] = set()
    # Longest display_name first to avoid prefix collisions during matching
    agents = sorted(registry.iter_enabled(), key=lambda c: len(c.display_name), reverse=True)
    # Build the set of agents to exclude: self + all upstream agents in the chain
    upstream = _upstream_agents or set()
    upstream.add(current_agent_id)

    with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
        _f.write(f"  agents: {[(c.agent_id, c.display_name, c.feishu_bot_open_id[:12] if c.feishu_bot_open_id else 'EMPTY') for c in agents]}\n")
        _f.write(f"  upstream (excluded): {upstream}\n")
    for cfg in agents:
        if cfg.agent_id in upstream:
            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"  SKIP agent {cfg.agent_id}: in upstream chain\n")
            continue
        if not cfg.feishu_bot_open_id:
            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"  SKIP agent {cfg.agent_id}: no bot_open_id\n")
            continue
        for name in (cfg.display_name, cfg.agent_id):
            if not name:
                continue
            pattern = rf"(^|[\s\W])@{re.escape(name)}(\s+|[，。！!,.:：*]|$)"
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match and cfg.agent_id not in seen:
                targets.append((cfg.agent_id, cfg.display_name, cfg.feishu_bot_open_id, match.start()))
                seen.add(cfg.agent_id)
                with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                    _f.write(f"  MATCH: agent={cfg.agent_id} name={name} pos={match.start()} open_id={cfg.feishu_bot_open_id[:16]}...\n")
                break

    # Sort targets by their @mention position in text (first mentioned = first executed)
    targets.sort(key=lambda t: t[3])
    # Strip the position field — downstream code expects 3-tuples
    targets = [(t[0], t[1], t[2]) for t in targets]

    with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
        _f.write(f"  targets count: {len(targets)}, order: {[t[0] for t in targets]}\n")
    if not targets:
        return

    # Check if gateway supports rich-text mentions
    if not hasattr(gateway, "send_message_with_mentions"):
        with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
            _f.write(f"  gateway has NO send_message_with_mentions\n")
        return

    # Build the mention list and send one message that @mentions all target bots.
    # The message text is the agent's original reply so target agents see full context.
    mentions = [{"open_id": bot_open_id, "name": display_name} for _, display_name, bot_open_id in targets]
    agent_names = ", ".join(f"@{n}" for _, n, _ in targets)
    with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
        _f.write(f"  SENDING delegation message, mentions={mentions}\n")

    # Build a short trigger message with task order for multi-agent delegation
    trigger_parts = [f"@{n}" for _, n, _ in targets]
    if len(targets) > 1:
        order_hints = " → ".join(f"@{n}" for _, n, _ in targets)
        trigger_text = f"任务分配顺序：{order_hints}。请按顺序接手处理。"
    else:
        trigger_text = " ".join(trigger_parts) + " 上面的任务交给你了，请接手处理。"

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
    # So we wake the target agent directly in-process: route a synthetic message
    # to each target agent so it picks up the task.
    # Targets are executed SEQUENTIALLY so downstream agents see upstream output.
    import time as _time
    from ocl.gateway.router import route_message as _route_message

    for idx, (target_agent_id, target_display_name, _target_open_id) in enumerate(targets):
        try:
            # Freshness-hold: check if channel has new messages since this agent started.
            # If yes, prepend a note so the target agent knows the context may be stale.
            fresh_note = ""
            try:
                store = await get_store(gateway.tenant_id, channel_id)
                last_seq = await store.get_last_seq()
                if last_seq > 0:
                    fresh_note = f"(频道当前最新消息 seq={last_seq}) "
            except Exception:
                pass

            # Build task-specific message so each agent knows its role and order
            if len(targets) > 1:
                task_msg = (
                    f"我是 @{current_agent_id}。上面的任务需要你来接手。"
                    f"你是第 {idx+1}/{len(targets)} 个被分配的 agent，"
                    f"请查看上面的对话内容并开始处理你负责的部分。"
                )
            else:
                task_msg = (
                    f"我是 @{current_agent_id}，上面的任务需要你来接手。"
                    f"请查看上面的对话内容并开始处理。"
                )

            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"  WAKING target agent={target_agent_id} (depth={_depth+1}, order={idx+1}/{len(targets)})\n")
            await _route_message(
                gateway=gateway,
                tenant_id=gateway.tenant_id,
                chat_id=channel_id,
                user_id=f"agent:{current_agent_id}",
                text=task_msg,
                message_id=f"delegate_{_time.time()}_{idx}",
                agent_id=target_agent_id,
                _delegation_depth=_depth + 1,
                _upstream_agents=set(upstream),  # pass a copy of the chain
            )
        except Exception:
            with open("/tmp/ocl_delegation_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"  WAKE FAILED for agent={target_agent_id}\n")
            logger.exception("Failed to wake target agent %s", target_agent_id)
