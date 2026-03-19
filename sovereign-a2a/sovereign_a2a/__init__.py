"""Sovereign A2A — JSON-RPC 3.0 message primitives for Sovereign AI agent communications.

Single source of truth for A2A 3.0 wire format across:
  sovereign-core, nanobot-01, a2a-browser, and any future agent.

No external dependencies — stdlib only.
"""

from .message import A2AMessage, A2AErrorCodes, A2AResponse

__all__ = ["A2AMessage", "A2AErrorCodes", "A2AResponse"]
__version__ = "0.1.0"
