"""ShapeMiner: Layer 1 OBSERVE — mine recurring wizard bigrams from traces.db."""
from __future__ import annotations
import sqlite3
from collections import defaultdict
from pathlib import Path


class ShapeMiner:
    def __init__(self, traces_db_path: str | Path) -> None:
        self.db_path = Path(traces_db_path)

    def expose_candidates(self, min_count: int = 3) -> list[dict]:
        """Return recurring bigrams sorted by frequency desc.

        Returns list of {sequence: "A -> B", count: int, avg_duration: float}.
        """
        if not self.db_path.exists():
            return []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, parent_id, wizard, started_at, ended_at, status "
                "FROM traces WHERE status='ok' ORDER BY started_at"
            ).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

        # Build parent->children map
        children: dict[str | None, list[dict]] = defaultdict(list)
        by_id: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            by_id[d["id"]] = d
            children[d["parent_id"]].append(d)

        # Walk sessions: group all completed traces into chains of siblings
        # Bigrams = consecutive wizard pairs within the same parent group
        bigrams: dict[tuple, list[float]] = defaultdict(list)
        for parent_id, siblings in children.items():
            siblings_sorted = sorted(siblings, key=lambda x: x["started_at"] or 0)
            for i in range(len(siblings_sorted) - 1):
                a = siblings_sorted[i]["wizard"]
                b = siblings_sorted[i + 1]["wizard"]
                key = (a, b)
                # duration of the pair window
                t0 = siblings_sorted[i]["started_at"] or 0
                t1 = siblings_sorted[i + 1]["ended_at"] or t0
                bigrams[key].append(t1 - t0)

        results = []
        for (a, b), durations in bigrams.items():
            if len(durations) >= min_count:
                avg = sum(durations) / len(durations)
                results.append({
                    "sequence": f"{a} -> {b}",
                    "count": len(durations),
                    "avg_duration": round(avg, 2),
                })
        return sorted(results, key=lambda x: -x["count"])
