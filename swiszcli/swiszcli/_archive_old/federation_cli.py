"""swiszcli federation: export/import learned examples between machines."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

def main(argv=None):
    parser = argparse.ArgumentParser(prog="swiszcli-federate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export", help="Dump learned examples to JSON")
    pe.add_argument("output", type=Path)
    pe.add_argument("--all", action="store_true", help="Include seeds + federated, not just learned")
    pe.add_argument("--db", type=Path, default=None)
    pi = sub.add_parser("import", help="Load JSON of examples from another machine")
    pi.add_argument("input", type=Path)
    pi.add_argument("--trust", type=float, default=0.5)
    pi.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    from .context_store import ContextStore
    from . import federation as fed

    store = ContextStore(db_path=args.db)
    if args.cmd == "export":
        sf = None if args.all else "learned"
        stats = fed.export_examples(store, args.output, source_filter=sf)
        print("exported", stats.exported, "examples to", args.output)
    else:
        stats = fed.import_examples(store, args.input, trust=args.trust)
        print("imported:", stats.imported, "skipped dup:", stats.skipped_dup, "errors:", stats.errors)
    store.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
