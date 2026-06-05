"""Top-level entrypoint for theSwiszard monorepo."""
from __future__ import annotations

import sys
from pathlib import Path


def _ensure_paths() -> None:
    root = Path(__file__).resolve().parent
    swiszcli_root = root / "swiszcli"
    if str(swiszcli_root) not in sys.path:
        sys.path.insert(0, str(swiszcli_root))


def main() -> None:
    _ensure_paths()
    # Defer import to keep startup fast and avoid side-effects in module import.
    from swiszcli.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
