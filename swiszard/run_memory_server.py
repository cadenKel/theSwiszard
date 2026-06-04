#!/usr/bin/env python3
"""
Run the swiszard memory server.

  python run_memory_server.py [--port 7437] [--host 127.0.0.1]
"""
import argparse
import logging
import sys
from pathlib import Path

# Allow running from repo root or from memory_server/ dir
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7437)
    args = parser.parse_args()

    uvicorn.run(
        "memory_server.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
