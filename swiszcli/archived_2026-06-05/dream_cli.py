"""swiszcli dream command: runs one dream_cycle."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="swiszcli-dream")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    from .context_store import ContextStore
    from . import dream_cycle as dc

    store = ContextStore(db_path=args.db)
    cfg = dc.DreamConfig.load(args.config)

    nl = chr(10)
    if not args.quiet:
        sys.stderr.write("swiszcli dream" + nl)
        sys.stderr.write("  config: promote>=" + str(cfg.promote_threshold) + " prune>=" + str(cfg.prune_days) + "d" + nl)
        sys.stderr.write("  dep: min_losses=" + str(cfg.dep_min_losses) + " ratio=" + str(cfg.dep_loss_ratio) + nl)
        sys.stderr.write("  swizmem: " + cfg.swizmem_url + nl)
        sys.stderr.write("  dry_run: " + str(args.dry_run) + nl)
        sys.stderr.write("  stats before: " + str(store.stats()) + nl)

    report = dc.run(store, config=cfg, dry_run=args.dry_run, log_path=args.log)

    if args.as_json:
        out = {
            "summary": report.summary(),
            "promoted": report.promoted,
            "pruned": report.pruned_count,
            "deprecated": report.deprecated,
            "errors": report.errors,
            "dry_run": args.dry_run,
        }
        print(json.dumps(out, indent=2))
    elif not args.quiet:
        sys.stderr.write("  report: " + str(report.summary()) + nl)
        if report.errors:
            sys.stderr.write("  ERRORS: " + str(report.errors) + nl)
            store.close()
            return 1
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
