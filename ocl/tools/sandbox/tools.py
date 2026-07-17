"""Sandbox tools — LiteLLM-compatible schemas and handler for agent use.

Tools exposed to agents:
  - exec_code: Execute code (Python/JS/TS/Java/Go/Bash) in an isolated sandbox
  - sandbox_read_file: Read a file from the sandbox
  - sandbox_write_file: Write a file to the sandbox
  - sandbox_list_files: List files in a sandbox directory
  - sandbox_install_package: Install a package (pip/npm) in the sandbox
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocl.tools.sandbox.provider import get_provider

if TYPE_CHECKING:
    from ocl.runtime.context import AgentRuntime

logger = logging.getLogger(__name__)

SANDBOX_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "exec_code",
            "description": (
                "Execute code in an isolated sandbox. Supports Python, JavaScript, "
                "TypeScript, Java, Go, and Bash. The sandbox persists across calls "
                "within the same session, so variables and files are retained.\n"
                "Use for: data analysis, calculations, file processing, running scripts, "
                "generating charts, validating code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The code to execute",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "javascript", "typescript", "java", "go", "bash", "shell"],
                        "description": "Programming language (default: python)",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_read_file",
            "description": "Read the content of a file from the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file in the sandbox",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_write_file",
            "description": "Write content to a file in the sandbox. Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path for the file in the sandbox",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_list_files",
            "description": "List files and directories in a sandbox path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (default: /)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_install_package",
            "description": "Install a package in the sandbox (pip for Python, npm for JS/TS).",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name (e.g. 'pandas', 'numpy', 'axios')",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "javascript", "typescript"],
                        "description": "Package ecosystem (default: python)",
                    },
                },
                "required": ["package"],
            },
        },
    },
]


class SandboxHandler:
    """Handles sandbox_* and exec_code tools.

    Delegates to SandboxProvider which manages the OpenSandbox SDK.
    Sandboxes are reused per session_id.
    """

    schemas = SANDBOX_TOOL_SCHEMAS

    async def run(self, rt: "AgentRuntime", name: str, args: dict) -> str:
        provider = get_provider()
        session_id = rt.session_id or rt.channel_id

        try:
            if name == "exec_code":
                code = args.get("code", "")
                language = args.get("language", "python")
                if not code:
                    return "Error: no code provided."
                return await provider.exec_code(session_id, code, language)

            if name == "sandbox_read_file":
                path = args.get("path", "")
                if not path:
                    return "Error: no path provided."
                return await provider.read_file(session_id, path)

            if name == "sandbox_write_file":
                path = args.get("path", "")
                content = args.get("content", "")
                if not path:
                    return "Error: no path provided."
                return await provider.write_file(session_id, path, content)

            if name == "sandbox_list_files":
                path = args.get("path", "/")
                return await provider.list_files(session_id, path)

            if name == "sandbox_install_package":
                package = args.get("package", "")
                language = args.get("language", "python")
                if not package:
                    return "Error: no package name provided."
                return await provider.install_package(session_id, package, language)

            return f"Unknown sandbox tool: {name}"

        except RuntimeError as e:
            # Sandbox not enabled or connection error
            return f"Sandbox error: {e}"
        except Exception as e:
            logger.exception("Sandbox tool %s failed", name)
            return f"Sandbox tool '{name}' failed: {type(e).__name__}: {e}"
