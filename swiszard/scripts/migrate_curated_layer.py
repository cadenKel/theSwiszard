#!/usr/bin/env python3
"""
Migrate ~/.hermes/memories/{MEMORY.md,USER.md} into swiszmem as pinned
'always_inject' memories. Idempotent: skips entries whose content already
exists verbatim in an active pinned memory.
"""
import json, sys, sqlite3
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:7437"
DB = Path.home() / ".hermes" / "swiszard" / "memory.db"
SRC = {
    "memory": Path.home() / ".hermes" / "memories" / "MEMORY.md",
    "user":   Path.home() / ".hermes" / "memories" / "USER.md",
}

def post(path, payload):
    req = Request(BASE + path, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def existing_pinned():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT content FROM memories WHERE deprecated=0 AND tags LIKE '%always_inject%'"
    ).fetchall()
    conn.close()
    return {r[0].strip() for r in rows}

def main():
    pinned = existing_pinned()
    total = skipped = 0
    migrated = []
    for target, path in SRC.items():
        if not path.exists():
            print(f"skip {target}: {path} missing"); continue
        entries = [e.strip() for e in path.read_text().split("\n§\n") if e.strip()]
        for entry in entries:
            total += 1
            if entry in pinned:
                skipped += 1; continue
            tags = ["curated_layer", f"curated_{target}"]
            r = post("/remember", {"content": entry, "kind": "fact", "session_id": "curated_layer", "source": "curated_layer_migration", "tags": tags})
            mid = r.get("memory_id") or r.get("id")
            if not mid:
                print(f"FAIL no id: {r}"); sys.exit(1)
            post("/pin", {"memory_id": mid})
            migrated.append((target, mid))
            print(f"  pinned id={mid} [{target}] {entry[:60]}...")
    print(f"\nDone. total={total} skipped={skipped} migrated={len(migrated)}")

if __name__ == "__main__":
    main()
