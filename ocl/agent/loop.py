"""Agent loop — ReAct-style: think -> tool call -> observe -> repeat -> reply -> memory curation.

Supports multi-agent mode: each invocation carries an agent_id that determines
which agent's AGENT.md, MEMORY.md, and tool set to use.

Tool dispatch is handled by the ToolDispatcher (ocl.runtime.dispatcher), which
replaces the previous if/elif chain. Delegation logic lives in
ocl.runtime.delegation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ocl.agent.context import build_messages, build_system_prompt
from ocl.agent.skills import maybe_create_skill
from ocl.agents.config import AgentConfig, load_agents
from ocl.agents.lifecycle import wake_agent
from ocl.config import settings
from ocl.llm import acompletion, acompletion_stream, vision_acompletion
from ocl.memory.compactor import compact_memory
from ocl.memory.mem0_store import mem0_add, mem0_search
from ocl.memory.store import MessageStore, get_store
from ocl.memory.writer import run_memory_curation
from ocl.runtime.context import AgentRuntime
from ocl.runtime.delegation import maybe_delegate_to_agents, trigger_downstream
from ocl.runtime.dispatcher import get_dispatcher
from ocl.tools import registry as _tool_registry

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
    _delegation_task_id: int | None = None,
    _session_id: str = "",
) -> None:
    # Create cancellation token and ledger entry for this run
    from ocl.agents.cancel import create_cancel_token, remove_cancel_token, is_cancelled
    from ocl.agents.ledger import LedgerEntry, create_entry as ledger_create, finalize_entry as ledger_finalize

    cancel_token = create_cancel_token(channel_id, agent_id)

    # Task session: one session spans the whole big-task — the user's message
    # starts a new session; every delegated agent in the chain inherits it so
    # task chaining, task-layer memory, and session filtering all work.
    from ocl.agents.task_store import new_session_id
    _task_session_id = _session_id or new_session_id()
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

    # Build unified runtime context — replaces 12+ individual parameters
    rt = AgentRuntime(
        channel_id=channel_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        session_id=_task_session_id,
        user_id=user_id,
        display_name=display_name,
        gateway=gateway,
        store=store,
        ledger_entry=ledger_entry,
        cancel_token=cancel_token,
        delegation_depth=_delegation_depth,
        upstream_agents=_upstream_agents or set(),
        delegation_task_id=_delegation_task_id,
        dispatcher=get_dispatcher(),
    )

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
    system_prompt = await build_system_prompt(
        channel_id, user_map,
        agent_config=agent_config, mem0_recall=mem0_recall,
        task_text=text, session_id=_task_session_id,
    )
    messages = await build_messages(channel_id, user_id, display_name, text, message_id, store, agent_id=agent_id)

    if image_base64:
        _attach_image_to_last_message(messages, image_base64)

    # Check for existing checkpoint — resume from crash/interrupt
    _resume_round = 0
    from ocl.runtime.checkpoint import get_checkpoint_manager
    _saved_state = await get_checkpoint_manager().resume(rt)
    if _saved_state is not None:
        logger.info(
            "Resuming from checkpoint: round %d, %d messages (channel=%s agent=%s)",
            _saved_state["round_num"], len(_saved_state["messages"]),
            channel_id, agent_id,
        )
        messages = _saved_state["messages"]
        _resume_round = _saved_state["round_num"]

    tools = rt.list_tools()

    tool_call_count = 0
    _has_task_create = False  # Track if agent called task_create
    final_text = ""
    streamed_message_id: str | None = None

    for _round in range(_resume_round, MAX_TOOL_ROUNDS):
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

        # Context compression for long conversations — each round before LLM call
        from ocl.runtime.context_manager import get_context_manager
        messages = await get_context_manager().maybe_compress(rt, messages)

        # Save checkpoint at the start of each round (for crash recovery)
        from ocl.runtime.checkpoint import get_checkpoint_manager
        # Include sandbox_id so we can reattach to the sandbox after restart
        from ocl.tools.sandbox.provider import get_provider as _get_sandbox_provider
        _sandbox_id = ""
        _provider = _get_sandbox_provider()
        if _task_session_id in _provider._sandboxes:
            _sandbox = _provider._sandboxes[_task_session_id]
            _sandbox_id = getattr(_sandbox, "id", "")
        await get_checkpoint_manager().save(rt, messages, _round, _sandbox_id)

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
                        chat_id=channel_id,
                    )
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
                    if fn_name == "task_create":
                        _has_task_create = True

                    logger.info("Tool call: %s(%s) in channel=%s agent=%s", fn_name, fn_args, channel_id, agent_id)

                    result = await rt.exec_tool(fn_name, fn_args)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })
                # Fill the inter-round gap: while the next LLM call is in flight
                # (several seconds of latency), show what was just executed and
                # what the agent is thinking about next — never a blank/stale card.
                if streamed_message_id:
                    try:
                        _done_tools = ", ".join(
                            tc["function"]["name"] for tc in tool_calls_chunks if tc["function"]["name"]
                        )
                        _hint = await _next_step_hint(channel_id, agent_id, _delegation_task_id)
                        await gateway.update_card_message(
                            message_id=streamed_message_id,
                            text=f"⚙️ 已执行: {_done_tools}\n💭 {_hint}",
                            agent_id=agent_id,
                        )
                    except Exception:
                        pass  # Card update is cosmetic — never block the loop

                # Keep streamed_message_id so next round UPDATES the same card
                # (instead of sending a new one). Only clear the text buffers.
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

            # Record token usage from non-streaming response
            if hasattr(response, "usage") and response.usage:
                ledger_entry.add_token_usage(
                    prompt=getattr(response.usage, "prompt_tokens", 0) or 0,
                    completion=getattr(response.usage, "completion_tokens", 0) or 0,
                )

            if not msg.tool_calls:
                final_text = msg.content or getattr(msg, "reasoning_content", "") or ""
                break

            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                tool_call_count += 1
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or "{}")
                if fn_name == "task_create":
                    _has_task_create = True

                logger.info("Tool call: %s(%s) in channel=%s agent=%s", fn_name, fn_args, channel_id, agent_id)

                result = await rt.exec_tool(fn_name, fn_args)

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

    # Agent delegation: trigger if the agent explicitly delegated OR if the
    # reply @mentions another agent but no tasks were created (runtime fallback).
    _should_delegate = "@delegate:" in final_text or "请接手处理" in final_text

    # Runtime fallback: if the agent @mentioned another agent in its reply
    # but didn't create any tasks, auto-create tasks and delegate.
    # Triggers even if the agent called other tools (task_list etc.) —
    # as long as it didn't call task_create, the runtime fills the gap.
    if not _should_delegate and not _has_task_create and _delegation_depth == 0:
        _should_delegate = await _auto_delegate_from_mentions(
            gateway=gateway,
            channel_id=channel_id,
            text=final_text,
            current_agent_id=agent_id,
            session_id=_task_session_id,
            reply_to=message_id,
        )

    if _should_delegate:
        await maybe_delegate_to_agents(
            gateway=gateway,
            channel_id=channel_id,
            text=final_text,
            current_agent_id=agent_id,
            reply_to=message_id,
            session_id=_task_session_id,
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

    # Clear checkpoint — task completed successfully
    from ocl.runtime.checkpoint import get_checkpoint_manager
    await get_checkpoint_manager().clear(channel_id, agent_id, _task_session_id)

    # Destroy sandbox for this session — session is done
    from ocl.tools.sandbox.provider import get_provider as _get_sandbox_provider
    await _get_sandbox_provider().destroy(_task_session_id)

    # Event-driven delegation: trigger downstream agents if this was a delegated task
    if _delegation_task_id is not None:
        asyncio.create_task(trigger_downstream(
            gateway=gateway,
            channel_id=channel_id,
            completed_task_id=_delegation_task_id,
            completed_agent_id=agent_id,
            _depth=_delegation_depth,
            _upstream_agents=_upstream_agents,
        ))


async def _auto_delegate_from_mentions(
    gateway,
    channel_id: str,
    text: str,
    current_agent_id: str,
    session_id: str,
    reply_to: str,
) -> bool:
    """Runtime fallback detector: should we delegate based on @mentions?

    Returns True when the reply @mentions another agent AND no tasks exist
    yet in this session (so the caller triggers maybe_delegate_to_agents,
    which owns task creation + waking). Does NOT create tasks itself —
    single creation path lives in maybe_delegate_to_agents.
    """
    import re

    from ocl.agents.config import load_agents
    from ocl.agents.task_store import task_list_by_session

    try:
        registry = load_agents(channel_id)
    except Exception:
        return False

    # Find @mentioned agents in the reply text
    for cfg in registry.iter_enabled():
        if cfg.agent_id == current_agent_id:
            continue
        if not cfg.feishu_bot_open_id:
            continue
        for name in (cfg.display_name, cfg.agent_id):
            if not name:
                continue
            pattern = rf"(^|[\s\W])@{re.escape(name)}(\s+|[，。！!,.:：*]|$)"
            if re.search(pattern, text, flags=re.IGNORECASE):
                # Check if tasks already exist in this session (avoid duplicates)
                if session_id:
                    try:
                        existing = await task_list_by_session(channel_id, session_id)
                    except Exception:
                        existing = []
                    if existing:
                        return False  # Tasks already created
                logger.info(
                    "Auto-delegating from @mentions (no tasks created by agent)"
                )
                return True

    return False


async def _next_step_hint(channel_id: str, agent_id: str, delegation_task_id: int | None) -> str:
    """Build a contextual 'thinking next' hint for the streaming card gap.

    Uses the current task title so the user sees e.g. '思考：撰写闹钟App的PRD文档…'
    instead of a blank card between tool rounds.
    """
    try:
        from ocl.agents.task_store import task_get, task_list

        # Prefer the task this agent was delegated to execute
        if delegation_task_id:
            task = await task_get(channel_id, delegation_task_id)
            if task and task.get("title"):
                return f"思考：{task['title']}…"

        # Otherwise: this agent's latest in-progress task
        tasks = await task_list(channel_id, status="in_progress", assignee=agent_id)
        if tasks:
            return f"思考：{tasks[-1]['title']}…"
    except Exception:
        pass
    return "正在思考下一步…"


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
