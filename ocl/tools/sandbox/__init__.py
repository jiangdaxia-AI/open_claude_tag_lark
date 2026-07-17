"""OpenSandbox integration — isolated code execution for agents.

Provides the SandboxProvider (SDK wrapper), tool schemas + handler, and
lifecycle management (timeout cleanup, graceful shutdown).

Agent calls exec_code / sandbox_read_file / sandbox_write_file / etc. through
the ToolDispatcher. The provider reuses one sandbox per session_id.
"""

from ocl.tools.sandbox.provider import SandboxProvider, get_provider
from ocl.tools.sandbox.tools import SANDBOX_TOOL_SCHEMAS, SandboxHandler

__all__ = ["SandboxProvider", "get_provider", "SANDBOX_TOOL_SCHEMAS", "SandboxHandler"]
