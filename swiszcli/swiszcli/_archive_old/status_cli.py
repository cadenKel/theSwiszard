"""swiszcli status command — inspect contexts.db, swizmem health, recent dream_cycle."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="swiszcli-status")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--swizmem-url", default="http://127.0.0.1:7437")
    parser.add_argument("--dream-log", type=Path, default=None)
    args = parser.parse_args(argv)

    from .context_store import ContextStore, DEFAULT_DB_PATH

    report = {}
    db_path = args.db or DEFAULT_DB_PATH
    if db_path.exists():
        store = ContextStore(db_path=db_path)
        s = store.stats()
        # top wizards by example count
        rows = store._conn.execute(
            "SELECT wizard_name, COUNT(*) c, SUM(wins) w, SUM(losses) l FROM examples GROUP BY wizard_name ORDER BY c DESC"
        ).fetchall()
        wizards = [dict(r) for r in rows]
        # recent sessions
        rows2 = store._conn.execute(
            "SELECT session_id, COUNT(*) chunks, MAX(ts) last_seen FROM chunks GROUP BY session_id ORDER BY last_seen DESC LIMIT 5"
        ).fetchall()
        sessions = [dict(r) for r in rows2]
        report["contexts_db"] = {"path": str(db_path), "stats": s, "wizards": wizards, "recent_sessions": sessions}
        store.close()
    else:
        report["contexts_db"] = {"path": str(db_path), "exists": False}

    # swizmem health
    try:
        import httpx
        r = httpx.get(args.swizmem_url + "/health", timeout=2.0)
        report["swizmem"] = {"url": args.swizmem_url, "ok": r.status_code == 200, "resp": r.json()}
    except Exception as e:
        report["swizmem"] = {"url": args.swizmem_url, "ok": False, "error": str(e)}

    # dream_cycle last log
    log_path = args.dream_log or Path.home() / ".swiszcli" / "dream_cycle.log"
    if log_path.exists():
        try:
            lines_in = [l for l in log_path.read_text().splitlines() if l.strip()]
            last = json.loads(lines_in[-1]) if lines_in else None
            report["dream_cycle"] = {"log": str(log_path), "runs_total": len(lines_in), "last_run": last}
        except Exception as e:
            report["dream_cycle"] = {"log": str(log_path), "error": str(e)}
    else:
        report["dream_cycle"] = {"log": str(log_path), "exists": False, "hint": "never run; install timer or run swiszcli-dream"}

    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        nl = chr(10)
        sys.stdout.write("swiszcli status" + nl)
        sys.stdout.write("=" * 50 + nl)
        cdb = report["contexts_db"]
        if cdb.get("stats"):
            sys.stdout.write("contexts.db: " + cdb["path"] + nl)
            for k, v in cdb["stats"].items():
                sys.stdout.write("  " + k + ": " + str(v) + nl)
            sys.stdout.write("  wizards:" + nl)
            for w in cdb["wizards"]:
                sys.stdout.write("    " + str(w["wizard_name"]) + ": " + str(w["c"]) + " examples, " + str(w["w"] or 0) + "w/" + str(w["l"] or 0) + "l" + nl)
        else:
            sys.stdout.write("contexts.db: NOT INITIALIZED (" + cdb["path"] + ")" + nl)
        sys.stdout.write(nl + "swizmem: " + str(report["swizmem"]) + nl)
        sys.stdout.write(nl + "dream_cycle:" + nl)
        dc = report["dream_cycle"]
        if dc.get("runs_total"):
            sys.stdout.write("  total runs: " + str(dc["runs_total"]) + nl)
            lr = dc.get("last_run") or {}
            if lr:
                sys.stdout.write("  last run summary: " + str(lr.get("summary")) + nl)
        else:
            sys.stdout.write("  " + str(dc.get("hint", "no runs yet")) + nl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
