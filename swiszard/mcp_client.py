'''Thin HTTP client for the central Swiszard MCP server.

Usage:
    from swiszard.mcp_client import client
    result = client.call("swiszard_do", task="project status swiszard")
'''

from __future__ import annotations

import os
import json

import httpx

DEFAULT_BASE = "http://127.0.0.1:8743"
TIMEOUT = float(os.environ.get("SWISZARD_MCP_TIMEOUT", "120"))


class MCPClient:
    def __init__(self, base_url: str | None = None):
        self.base = (base_url or DEFAULT_BASE).rstrip("/")

    def call(self, tool: str, **kwargs) -> str:
        """Call a tool by name. Returns the text response."""
        url = f"{self.base}/{tool}"
        r = httpx.post(url, json=kwargs, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text


# Singleton default client
client = MCPClient()
