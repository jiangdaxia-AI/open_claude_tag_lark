"""Runtime abstraction layer — AgentRuntime, ToolDispatcher, handlers, delegation.

This module provides the unified runtime context that replaces the 12+ parameter
pass-through pattern in loop.py. All tools are dispatched through a single
ToolDispatcher, eliminating the if/elif chain.
"""

from ocl.runtime.context import AgentRuntime
from ocl.runtime.dispatcher import ToolDispatcher, ToolHandler, get_dispatcher

__all__ = ["AgentRuntime", "ToolDispatcher", "ToolHandler", "get_dispatcher"]
