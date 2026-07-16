"""Built-in tools always available to the agent: web search, Python runner, channel search."""

from __future__ import annotations

import asyncio
import logging
import sys
from io import StringIO
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# LiteLLM-compatible tool schemas
BUILTIN_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date information",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python snippet and return stdout. Use for calculations, data processing, and quick scripts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_channel_history",
            "description": "Full-text search across this channel's message history",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_append",
            "description": "Append a new fact or decision to channel long-term memory (MEMORY.md)",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The fact to persist (one concise bullet)"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_replace",
            "description": "Replace an outdated fact in channel memory with an updated version",
            "parameters": {
                "type": "object",
                "properties": {
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_artifact",
            "description": "Save a long-form output (PRD, report, code review) as a file artifact in the agent's workspace, then return a summary + file path. Use this instead of dumping long text in the chat reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File name (e.g. 'prd-v1.md')"},
                    "content": {"type": "string", "description": "Full content to save"},
                    "summary": {"type": "string", "description": "Short summary for the chat reply (1-3 sentences)"},
                },
                "required": ["filename", "content", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thread_unfollow",
            "description": "Stop receiving notifications for a thread. Call when your work in a thread is done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread to unfollow (message_id of the parent)"},
                },
                "required": ["thread_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bookmark_message",
            "description": "Bookmark/save a message for later reference",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The message ID to bookmark"},
                },
                "required": ["message_id"],
            },
        },
    },
]


async def dispatch_builtin(fn_name: str, args: dict[str, Any], channel_id: str = "", agent_id: str = "default", user_id: str = "", store=None) -> Any:
    if fn_name == "web_search":
        return await _web_search(args["query"])
    if fn_name == "run_python":
        return _run_python(args["code"])
    if fn_name == "save_artifact":
        return await _save_artifact(channel_id, agent_id, args["filename"], args["content"], args["summary"])
    if fn_name == "thread_unfollow":
        from ocl.agents.thread_follow import unfollow_thread
        await unfollow_thread(channel_id, args["thread_id"], agent_id)
        return f"Unfollowed thread {args['thread_id']}"
    if fn_name == "bookmark_message":
        if store:
            await store.add_bookmark(user_id, args["message_id"])
            return f"Bookmarked message {args['message_id']}"
        return "Bookmark not available (no store)"
    return f"Unknown built-in: {fn_name}"


async def _save_artifact(channel_id: str, agent_id: str, filename: str, content: str, summary: str) -> str:
    """Save content to agent workspace, return summary + path."""
    from ocl.agents.config import load_agents
    try:
        registry = load_agents(channel_id)
        cfg = registry.get(agent_id)
        if cfg:
            ws = cfg.ensure_workspace()
            file_path = ws / filename
            file_path.write_text(content, encoding="utf-8")
            return f"Artifact saved: {filename} ({len(content)} chars)\nPath: {file_path}\nSummary: {summary}"
    except Exception as e:
        return f"Failed to save artifact: {e}"
    return f"Could not find agent config for {agent_id}"


async def _web_search(query: str) -> str:
    # Uses DuckDuckGo instant answer API — no key required
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            )
            data = resp.json()
            abstract = data.get("AbstractText", "")
            related = [r.get("Text", "") for r in data.get("RelatedTopics", [])[:3] if r.get("Text")]
            if abstract:
                return abstract + ("\n\nRelated:\n" + "\n".join(f"- {r}" for r in related) if related else "")
            if related:
                return "\n".join(f"- {r}" for r in related)
            return "No results found."
    except Exception as e:
        logger.warning("Web search failed: %s", e)
        return f"Search failed: {e}"


def _run_python(code: str) -> str:
    # Sandboxed Python execution — stdout captured, dangerous builtins removed
    safe_globals: dict[str, Any] = {
        "__builtins__": {
            k: v for k, v in __builtins__.items()  # type: ignore[union-attr]
            if k not in ("open", "exec", "eval", "__import__", "compile")
        }
        if isinstance(__builtins__, dict)
        else {},
        "print": print,
    }
    # Allow common safe stdlib
    import math, json, re, datetime
    safe_globals.update({"math": math, "json": json, "re": re, "datetime": datetime})

    old_stdout = sys.stdout
    sys.stdout = buffer = StringIO()
    try:
        exec(code, safe_globals)  # noqa: S102
        output = buffer.getvalue()
        return output.strip() or "(no output)"
    except Exception as e:
        return f"Error: {e}"
    finally:
        sys.stdout = old_stdout
