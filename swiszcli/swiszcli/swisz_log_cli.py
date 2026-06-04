"""Inspect per-session swiszard call logs.

Usage:
    swiszcli-swisz-log                  # summary of latest session
    swiszcli-swisz-log latest           # ditto
    swiszcli-swisz-log list             # list all sessions newest first
    swiszcli-swisz-log show [SID]       # one-liner per call, latest if no SID
    swiszcli-swisz-log full [SID]       # full JSONL dump (jq-friendly)
    swiszcli-swisz-log errors [SID]     # only error rows
    swiszcli-swisz-log grep PATTERN [SID]
    swiszcli-swisz-log call CALL_ID     # one full record by call_id

SID can be a swisz_xxxxxxxx, \"latest\", or a unique prefix.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .config import Config


def _logdir():
    return Config().state_dir / "swisz_calls"


def _resolve(sid):
    d = _logdir()
    if not d.exists():
        sys.exit(f"no swisz_calls dir at {d}")
    if not sid or sid == "latest":
        latest = d / "latest"
        if latest.exists():
            return latest.resolve()
        files = sorted(d.glob("swisz_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            sys.exit("no session logs yet")
        return files[0]
    candidates = sorted(d.glob(f"{sid}*.jsonl"))
    if not candidates:
        candidates = sorted(d.glob(f"*{sid}*.jsonl"))
    if not candidates:
        sys.exit(f"no log matches {sid!r}")
    if len(candidates) > 1:
        sys.exit("ambiguous SID, matches: " + ", ".join(p.stem for p in candidates))
    return candidates[0]


def _iter(p):
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"# corrupt line skipped: {e}", file=sys.stderr)


def _trunc(s, n):
    if not isinstance(s, str):
        s = repr(s)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def cmd_list():
    d = _logdir()
    if not d.exists():
        sys.exit(f"no swisz_calls dir at {d}")
    files = sorted(d.glob("swisz_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("(no sessions)")
        return
    print("{:22} {:>6} {:>5} {:>8}  {}".format("session", "calls", "errs", "sz", "mtime"))
    for p in files:
        n = 0; errs = 0
        for r in _iter(p):
            n += 1
            if r.get("error"):
                errs += 1
        sz = p.stat().st_size
        mt = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{p.stem:22} {n:>6} {errs:>5} {sz:>8}  {mt}")


def cmd_summary(sid):
    p = _resolve(sid)
    rows = list(_iter(p))
    if not rows:
        print(f"{p.name}: empty")
        return
    handlers = {}
    errs = 0
    total_ms = 0
    for r in rows:
        h = r.get("handler", "?")
        handlers[h] = handlers.get(h, 0) + 1
        if r.get("error"):
            errs += 1
        total_ms += int(r.get("duration_ms") or 0)
    print(f"file: {p}")
    print(f"calls: {len(rows)}   errors: {errs}   total: {total_ms} ms")
    print("handlers:")
    for h, cnt in sorted(handlers.items(), key=lambda kv: -kv[1]):
        print(f"  {cnt:>4}  {h}")
    print()
    print("first 3 calls:")
    for r in rows[:3]:
        h = r.get("handler", "?")
        print(f"  [{h}] {_trunc(r.get("task", ""), 90)}")
    if len(rows) > 3:
        print("last 3 calls:")
        for r in rows[-3:]:
            h = r.get("handler", "?")
            print(f"  [{h}] {_trunc(r.get("task", ""), 90)}")


def cmd_show(sid):
    p = _resolve(sid)
    print(f"# {p}")
    for i, r in enumerate(_iter(p), 1):
        marker = "E" if r.get("error") else " "
        cid = r.get("call_id", "")
        dur = r.get("duration_ms", 0)
        h = (r.get("handler") or "?")[:24]
        task = _trunc(r.get("task", ""), 60)
        res = _trunc(r.get("result", ""), 80)
        print(f"{i:>4} {marker} {cid:14} {dur:>6}ms  [{h:<24}]  task={task}  ->  {res}")


def cmd_full(sid):
    p = _resolve(sid)
    with open(p, "r", encoding="utf-8") as f:
        sys.stdout.write(f.read())


def cmd_errors(sid):
    p = _resolve(sid)
    n = 0
    for r in _iter(p):
        bad = r.get("error") or (isinstance(r.get("result"), str)
                                  and "no confident handler match" in r.get("result", ""))
        if bad:
            n += 1
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print("---")
    if not n:
        print("(no errors)")


def cmd_grep(pattern, sid):
    p = _resolve(sid)
    rx = re.compile(pattern, re.IGNORECASE)
    for r in _iter(p):
        blob = (r.get("task") or "") + "\n" + (r.get("result") or "")
        if rx.search(blob):
            h = r.get("handler", "?")
            print(f"{r.get("call_id")}  [{h}]  {_trunc(r.get("task", ""), 80)}")


def cmd_call(call_id):
    d = _logdir()
    for p in d.glob("swisz_*.jsonl"):
        for r in _iter(p):
            if r.get("call_id") == call_id:
                print(f"# session: {p.stem}")
                print(json.dumps(r, ensure_ascii=False, indent=2))
                return
    sys.exit(f"no call with id {call_id}")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "latest":
        cmd_summary(args[0] if args else None)
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd == "list":
        cmd_list()
    elif cmd == "show":
        cmd_show(rest[0] if rest else None)
    elif cmd == "full":
        cmd_full(rest[0] if rest else None)
    elif cmd == "errors":
        cmd_errors(rest[0] if rest else None)
    elif cmd == "summary":
        cmd_summary(rest[0] if rest else None)
    elif cmd == "grep":
        if not rest:
            sys.exit("grep needs PATTERN [SID]")
        cmd_grep(rest[0], rest[1] if len(rest) > 1 else None)
    elif cmd == "call":
        if not rest:
            sys.exit("call needs CALL_ID")
        cmd_call(rest[0])
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        cmd_summary(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
