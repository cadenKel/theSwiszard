#!/usr/bin/env python3
"""routes.db instrumentation dashboard."""

import sqlite3, os, time, math, struct
from collections import Counter, defaultdict
from pathlib import Path

DB = Path.home() / ".hermes" / "swiszard" / "routes.db"

def cosine(a, b):
    dad, dbb, dcc = 0.0, 0.0, 0.0
    for i in range(len(a)):
        dad += a[i] * b[i]
        dbb += a[i] * a[i]
        dcc += b[i] * b[i]
    return dad / math.sqrt(dbb * dcc) if dbb and dcc else 0.0

def blob_to_floats(blob):
    return list(struct.unpack(f"{len(blob)//4}f", blob))

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM examples ORDER BY id").fetchall()
    
    if not rows:
        print("No examples in routes.db yet.")
        return
    
    total = len(rows)
    
    print("=" * 60)
    print("  ROUTES.DB LEARNING DASHBOARD")
    print("=" * 60)
    
    created_times = [time.mktime(time.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")) for r in rows]
    first = min(created_times)
    last = max(created_times)
    hours = max((last - first) / 3600, 1)
    rate = total / hours
    
    print(f"  Bank size:    {total:4d} examples")
    print(f"  Growth rate:  {rate:6.1f}/hour (h={hours:.1f})")
    print(f"  First:        {time.strftime('%Y-%m-%d %H:%M', time.localtime(first))}")
    print(f"  Last:         {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}")
    
    wins_count = sum(1 for r in rows if r["success_count"] > 0)
    total_wins = sum(r["success_count"] for r in rows)
    total_losses = sum(r["fail_count"] for r in rows)
    win_ratio = total_wins / max(total_wins + total_losses, 1) * 100
    
    print(f"\n  Examples w/wins: {wins_count} ({wins_count/max(total,1)*100:.1f}%)")
    print(f"  Total success events:  {total_wins:4d}")
    print(f"  Total fail events:     {total_losses:4d}")
    print(f"  Win ratio:             {win_ratio:6.1f}%")
    
    print("\n  Per-Handler Stats")
    handlers = Counter(r["handler"] for r in rows)
    hwins = defaultdict(int)
    hloss = defaultdict(int)
    for r in rows:
        hwins[r["handler"]] += r["success_count"]
        hloss[r["handler"]] += r["fail_count"]
    
    for h in sorted(handlers.keys()):
        w = hwins[h]
        l = hloss[h]
        ratio = w / max(w + l, 1) * 100
        bar = "|" * int(ratio / 5)
        print(f"  {h:30s} {handlers[h]:4d} ex  {bar:20s} {ratio:5.1f}%")
    
    print("\n  Embedding Overlap")
    for h in handlers:
        h_rows = [r for r in rows if r["handler"] == h]
        if len(h_rows) <= 2:
            print(f"  {h:30s} too few for analysis")
            continue
        embeddings = []
        for r in h_rows:
            try:
                embeddings.append(blob_to_floats(r["embedding"]))
            except:
                pass
        dup_pairs = 0
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = cosine(embeddings[i], embeddings[j])
                if sim >= 0.95:
                    dup_pairs += 1
                    break
            if dup_pairs:
                break
        status = f"{dup_pairs} near-dupes" if dup_pairs else "clean"
        print(f"  {h:30s} {status}")
    
    print("\n  Top Winning Examples")
    scored = []
    for r in rows:
        if r["success_count"] > 0:
            scored.append((r, r["success_count"] / max(r["success_count"] + r["fail_count"], 1)))
    scored.sort(key=lambda x: x[1], reverse=True)
    for r, score in scored[:5]:
        phrase = r["phrasing"].replace("\n", " ")[:100]
        print(f'  #{r["id"]} {r["handler"]:25s} {score*100:5.1f}%  {phrase}')
    
    print("\n  Diagnostics")
    shell_pct = handlers["handler_shell"] / max(total, 1) * 100
    if shell_pct > 40:
        print(f"  WARNING: {shell_pct:.1f}% of examples are handler_shell")
        print("    Noisy complex tasks getting routed to shell handler.")
    else:
        print(f"  OK: Handler distribution balanced (shell={shell_pct:.1f}%)")
    
    if total_losses == 0 and total > 10:
        print("  WARNING: ZERO fail events recorded.")
        print("    Either everything succeeds or loss tracking is broken.")
    
    stale_cutoff = time.time() - 7 * 86400
    stale = [r for r in rows if time.mktime(time.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")) < stale_cutoff]
    if stale:
        print(f"  NOTE: {len(stale)} examples older than 7 days.")
    else:
        print("  OK: No stale examples (> 7 days).")
    
    print(f"\n  DB: {DB}")
    print("=" * 60)
    conn.close()

if __name__ == "__main__":
    main()
