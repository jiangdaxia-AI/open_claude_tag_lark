"""SandboxProvider — OpenSandbox SDK wrapper with per-session sandbox reuse.

Manages sandbox lifecycle: create on first use, reuse for subsequent calls
within the same session, destroy on session end or timeout.

All SDK calls are based on the official OpenSandbox Python SDK API:
  - Sandbox.create(image, timeout, env, connection_config)
  - sandbox.commands.run(cmd) -> Execution
  - sandbox.files.read_file(path) -> bytes
  - sandbox.files.write_files([WriteEntry(...)])
  - sandbox.files.list_directory(path) -> list[DirectoryListEntry]
  - CodeInterpreter.create(sandbox) -> CodeInterpreter
  - interpreter.codes.run(code, language) -> Execution
  - sandbox.kill()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from ocl.config import settings

if TYPE_CHECKING:
    from code_interpreter import CodeInterpreter
    from opensandbox import Sandbox

logger = logging.getLogger(__name__)


class SandboxProvider:
    """OpenSandbox SDK wrapper — reuses one sandbox per session_id.

    Thread-safety: designed for asyncio single-thread event loop.
    All methods are async and should be called from the same event loop.
    """

    def __init__(self) -> None:
        self._sandboxes: dict[str, "Sandbox"] = {}
        self._interpreters: dict[str, "CodeInterpreter"] = {}
        self._connection_config = self._build_connection_config()

    def _build_connection_config(self):
        """Build ConnectionConfig from settings, or None if sandbox is disabled."""
        if not settings.sandbox_enabled:
            return None
        from datetime import timedelta as _timedelta
        from opensandbox.config.connection import ConnectionConfig

        return ConnectionConfig(
            domain=settings.sandbox_domain,
            protocol=settings.sandbox_protocol,
            request_timeout=_timedelta(seconds=120),
        )

    async def get_or_create(self, session_id: str) -> "Sandbox":
        """Get existing sandbox for session, or create a new one.

        Raises RuntimeError if sandbox is not enabled.
        """
        if session_id in self._sandboxes:
            return self._sandboxes[session_id]

        if not settings.sandbox_enabled:
            raise RuntimeError(
                "Sandbox is not enabled. Set sandbox_enabled=true in config "
                "and start the OpenSandbox Server."
            )

        from opensandbox import Sandbox

        logger.info("Creating sandbox for session %s", session_id)
        sandbox = await Sandbox.create(
            settings.sandbox_image,
            timeout=timedelta(minutes=settings.sandbox_timeout_minutes),
            ready_timeout=timedelta(seconds=120),
            env={
                "PYTHON_VERSION": "3.11",
                "JAVA_VERSION": "17",
                "NODE_VERSION": "20",
                "GO_VERSION": "1.24",
            },
            entrypoint=["/opt/code-interpreter/code-interpreter.sh"],
            connection_config=self._connection_config,
            skip_health_check=True,  # health check fails in Docker bridge mode; rely on execd /ping
        )
        self._sandboxes[session_id] = sandbox
        logger.info("Sandbox created for session %s", session_id)
        return sandbox

    async def get_or_create_interpreter(self, session_id: str) -> "CodeInterpreter":
        """Get or create a CodeInterpreter for the session's sandbox."""
        if session_id in self._interpreters:
            return self._interpreters[session_id]

        sandbox = await self.get_or_create(session_id)
        from code_interpreter import CodeInterpreter

        # When skip_health_check=True, the execd daemon may still be starting.
        # Wait briefly to let the runtime initialize before connecting.
        if session_id not in self._interpreters:
            await asyncio.sleep(15)

        interpreter = await CodeInterpreter.create(sandbox)
        self._interpreters[session_id] = interpreter
        return interpreter

    async def exec_code(self, session_id: str, code: str, language: str = "python") -> str:
        """Execute code in the sandbox and return formatted output.

        Uses CodeInterpreter for structured result extraction (result text,
        stdout, stderr, exit code).
        """
        from code_interpreter import SupportedLanguage

        interpreter = await self.get_or_create_interpreter(session_id)
        lang = self._parse_language(language)
        result = await interpreter.codes.run(code, language=lang)
        return self._format_execution_result(result)

    async def exec_shell(self, session_id: str, command: str) -> str:
        """Execute a shell command in the sandbox."""
        sandbox = await self.get_or_create(session_id)
        execution = await sandbox.commands.run(command)
        return self._format_execution_result(execution)

    async def read_file(self, session_id: str, path: str) -> str:
        """Read a file from the sandbox. Returns content as string."""
        sandbox = await self.get_or_create(session_id)
        content = await sandbox.files.read_file(path)
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)

    async def write_file(self, session_id: str, path: str, content: str) -> str:
        """Write content to a file in the sandbox."""
        from opensandbox.models import WriteEntry

        sandbox = await self.get_or_create(session_id)
        await sandbox.files.write_files([WriteEntry(path=path, data=content)])
        return f"File written: {path} ({len(content)} chars)"

    async def list_files(self, session_id: str, path: str = "/") -> str:
        """List files in a directory in the sandbox.

        Uses exec_code with os.listdir() instead of sandbox.files.list_directory
        because the SDK's list_directory has a parsing bug (v0.1.14) where it
        tries to access .path on string entries.
        """
        code = f"import os; entries = os.listdir({path!r}); print('\\n'.join(entries))"
        return await self.exec_code(session_id, code, "python")

    async def install_package(self, session_id: str, package: str, language: str = "python") -> str:
        """Install a package in the sandbox."""
        if language.lower() in ("python", "py"):
            cmd = f"pip install {package}"
        elif language.lower() in ("javascript", "js", "typescript", "ts"):
            cmd = f"npm install {package}"
        else:
            return f"Unsupported language for package install: {language}"
        return await self.exec_shell(session_id, cmd)

    async def destroy(self, session_id: str) -> None:
        """Destroy the sandbox for a session. Safe to call if no sandbox exists."""
        self._interpreters.pop(session_id, None)
        sandbox = self._sandboxes.pop(session_id, None)
        if sandbox:
            try:
                await sandbox.kill()
                logger.info("Sandbox destroyed for session %s", session_id)
            except Exception:
                logger.warning("Failed to destroy sandbox for session %s", session_id)

    async def destroy_all(self) -> None:
        """Destroy all active sandboxes. Called on graceful shutdown."""
        session_ids = list(self._sandboxes.keys())
        for sid in session_ids:
            await self.destroy(sid)

    def _parse_language(self, language: str):
        """Map string language name to SupportedLanguage enum."""
        from code_interpreter import SupportedLanguage

        mapping = {
            "python": SupportedLanguage.PYTHON,
            "py": SupportedLanguage.PYTHON,
            "javascript": SupportedLanguage.JAVASCRIPT,
            "js": SupportedLanguage.JAVASCRIPT,
            "typescript": SupportedLanguage.TYPESCRIPT,
            "ts": SupportedLanguage.TYPESCRIPT,
            "java": SupportedLanguage.JAVA,
            "go": SupportedLanguage.GO,
            "bash": SupportedLanguage.BASH,
            "shell": SupportedLanguage.BASH,
            "sh": SupportedLanguage.BASH,
        }
        return mapping.get(language.lower(), SupportedLanguage.PYTHON)

    def _format_execution_result(self, result) -> str:
        """Format an Execution object into a readable string for the LLM."""
        parts = []

        # Result text (from CodeInterpreter — the expression result)
        if hasattr(result, "result") and result.result:
            for r in result.result:
                if hasattr(r, "text") and r.text:
                    parts.append(r.text)

        # stdout
        if hasattr(result, "logs") and result.logs:
            if result.logs.stdout:
                stdout = "\n".join(msg.text for msg in result.logs.stdout if msg.text)
                if stdout.strip():
                    parts.append(f"[stdout]\n{stdout}")
            if result.logs.stderr:
                stderr = "\n".join(msg.text for msg in result.logs.stderr if msg.text)
                if stderr.strip():
                    parts.append(f"[stderr]\n{stderr}")

        # Exit code (non-zero indicates error)
        if hasattr(result, "exit_code") and result.exit_code is not None and result.exit_code != 0:
            parts.append(f"[exit_code: {result.exit_code}]")

        return "\n".join(parts) if parts else "(no output)"


# ── Global provider singleton ────────────────────────────────────────────────

_global_provider: SandboxProvider | None = None


def get_provider() -> SandboxProvider:
    """Get or create the global SandboxProvider instance."""
    global _global_provider
    if _global_provider is None:
        _global_provider = SandboxProvider()
    return _global_provider
