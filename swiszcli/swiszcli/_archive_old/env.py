"""Load swiszcli's own ~/.swiszcli/.env into os.environ.

Like Hermes does with python-dotenv, but for swiszcli's own file.
Call once at startup — after this, os.environ has all the keys.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_swiszcli_env(state_dir: str | Path | None = None) -> Path:
    """Load ~/.swiszcli/.env into os.environ. Returns the path loaded."""
    if state_dir is None:
        state_dir = Path(os.environ.get("SWISZCLI_STATE_DIR", str(Path.home() / ".swiszcli")))
    else:
        state_dir = Path(state_dir)

    env_file = state_dir / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=True)
    return env_file
