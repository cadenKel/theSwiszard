#!/usr/bin/env python3
"""Swiszard MCP HTTP server — multi-session safe.

Listens on a TCP port via streamable-http transport so multiple Hermes sessions
can share one server process without stdio collisions.

Usage:
    python http_server.py [--port PORT] [--host HOST]
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the same mcp object with all tools registered
from hermes_integration.server import mcp

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Swiszard MCP HTTP server")
    parser.add_argument("--port", type=int, default=8743)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"Swiszard MCP HTTP server starting on {args.host}:{args.port}")
    mcp.run(transport="streamable-http")
