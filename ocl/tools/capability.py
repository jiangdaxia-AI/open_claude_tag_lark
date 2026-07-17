"""Capability discovery tools — let agents query their own available capabilities.

Tools:
  - list_capabilities: List all available tools with short descriptions
  - describe_capability: Get detailed usage info for a specific tool

This is especially useful at the start of complex tasks, so the agent knows
what it can do before planning its approach.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)

CAPABILITY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_capabilities",
            "description": (
                "List all tools and capabilities you currently have access to. "
                "Call this before starting a complex task to understand what you can do. "
                "Includes: built-in tools, task/reminder/cron tools, sandbox code execution, "
                "orchestration tools, and any MCP tools configured for this channel."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_capability",
            "description": (
                "Get detailed usage information for a specific tool, including "
                "its parameters and expected behavior."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The tool name (e.g. 'exec_code', 'plan_subtasks', 'web_search')",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


class CapabilityHandler:
    """Handles capability discovery tools.

    Uses the dispatcher's tool list to provide dynamic capability info.
    """

    schemas = CAPABILITY_TOOLS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        if name == "list_capabilities":
            return self._list_capabilities(rt)

        if name == "describe_capability":
            tool_name = args.get("name", "")
            if not tool_name:
                return "Error: no tool name provided."
            return self._describe_capability(rt, tool_name)

        return f"Unknown capability tool: {name}"

    def _list_capabilities(self, rt: "AgentRuntime") -> str:
        tools = rt.list_tools()
        if not tools:
            return "No tools available."

        lines = [f"You have access to {len(tools)} tools:\n"]

        # Group by category
        categories: dict[str, list[str]] = {
            "Code Execution (Sandbox)": [],
            "Task Orchestration": [],
            "Task Management": [],
            "Reminders & Scheduling": [],
            "Memory & Knowledge": [],
            "Search & Documents": [],
            "Other": [],
        }

        sandbox_names = {"exec_code", "sandbox_read_file", "sandbox_write_file", "sandbox_list_files", "sandbox_install_package"}
        orch_names = {"plan_subtasks", "run_subtask", "wait_subtasks", "get_subtask_status", "retry_subtask"}
        search_names = {"web_search", "search_channel_history", "feishu_doc_create", "feishu_doc_read"}
        memory_names = {"memory_append", "memory_replace", "memory_delete", "save_artifact"}

        for t in tools:
            fn = t["function"]
            tool_name = fn["name"]
            desc = fn.get("description", "")
            short_desc = desc[:80].replace("\n", " ")
            entry = f"  - {tool_name}: {short_desc}"

            if tool_name in sandbox_names:
                categories["Code Execution (Sandbox)"].append(entry)
            elif tool_name in orch_names:
                categories["Task Orchestration"].append(entry)
            elif tool_name.startswith("task_"):
                categories["Task Management"].append(entry)
            elif tool_name.startswith("reminder_") or tool_name in ("schedule_task", "list_crons", "cancel_cron"):
                categories["Reminders & Scheduling"].append(entry)
            elif tool_name in memory_names:
                categories["Memory & Knowledge"].append(entry)
            elif tool_name in search_names:
                categories["Search & Documents"].append(entry)
            else:
                categories["Other"].append(entry)

        for cat, items in categories.items():
            if items:
                lines.append(f"### {cat}")
                lines.extend(items)
                lines.append("")

        return "\n".join(lines)

    def _describe_capability(self, rt: "AgentRuntime", tool_name: str) -> str:
        tools = rt.list_tools()
        for t in tools:
            fn = t["function"]
            if fn["name"] == tool_name:
                desc = fn.get("description", "(no description)")
                params = fn.get("parameters", {})
                required = params.get("required", [])
                properties = params.get("properties", {})

                lines = [f"Tool: {tool_name}", f"Description: {desc}"]

                if properties:
                    lines.append("\nParameters:")
                    for pname, pinfo in properties.items():
                        ptype = pinfo.get("type", "any")
                        pdesc = pinfo.get("description", "")
                        req = " (required)" if pname in required else " (optional)"
                        lines.append(f"  - {pname} ({ptype}{req}): {pdesc}")
                else:
                    lines.append("\nParameters: none")

                return "\n".join(lines)

        return f"Tool '{tool_name}' not found. Call list_capabilities to see available tools."
