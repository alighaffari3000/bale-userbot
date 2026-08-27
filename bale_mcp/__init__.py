"""A read-only MCP server over a personal Bale account.

The tools deliberately do not mirror `BaleApp`'s fifty methods. An agent asks
"who is selling X" and "what happened in that group this week"; the surface is
those questions, sized to fit in a context window, and nothing that writes to
the account. See `bale_mcp.server` for the tools and the reasoning.
"""

from .server import build_server, main

__all__ = ("build_server", "main")
