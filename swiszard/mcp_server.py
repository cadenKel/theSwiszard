'''Central FastMCP server for the entire Swiszard ecosystem.

Imports the existing FastMCP instance from hermes_integration.server (which has
all the core tools registered via @mcp.tool() decorators).

Run with:
    .venv/bin/uvicorn swiszard.mcp_server:app --host 127.0.0.1 --port 8743
'''

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_integration.server import mcp  # type: ignore[import]

app = mcp.streamable_http_app()
