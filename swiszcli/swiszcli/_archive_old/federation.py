"""P3 federated patterns: local export/import of learned examples.

Pure file I/O. NO network calls, NO automatic github push. User chooses
manually whether to git push the exported JSON file to a shared registry.

Export: dump all learned examples (source=learned, weight, wins, losses) to
        a portable JSON file. Embeddings included (base64) so other machines
        can use them without re-embedding via the same model.
Import: load a JSON file, dedup against existing examples (skip if same
        (text, wizard_name) pair exists), insert as source=federated with
        weight scaled by trust factor.

Trust model: imported examples start with weight = source_weight * trust
(default trust=0.5). Seeds are weight=0.5, learned local is 1.0, federated
starts at 0.25-0.5. Local learning then promotes them via record_win.
"""
from __future__ import annotations
import base64
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FederationStats:
    exported: int = 0
    imported: int = 0
    skipped_dup: int = 0
    errors: int = 0


def export_examples(store, output_path, source_filter="learned"):
    """Dump examples to JSON. source_filter=None exports all (incl seeds)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if source_filter:
        rows = store._conn.execute(
            "SELECT id, text, embedding, wizard_name, source, weight, wins, losses FROM examples WHERE source = ?",
            (source_filter,),
        ).fetchall()
    else:
        rows = store._conn.execute(
            "SELECT id, text, embedding, wizard_name, source, weight, wins, losses FROM examples"
        ).fetchall()
    out = {
        "version": 1,
        "exported_at": time.time(),
        "embedding_model": "nomic-embed-text",
        "embedding_dim": 768,
        "source_filter": source_filter,
        "count": len(rows),
        "examples": [],
    }
    for r in rows:
        out["examples"].append({
            "text": r["text"],
            "wizard_name": r["wizard_name"],
            "embedding_b64": base64.b64encode(r["embedding"]).decode(),
            "source": r["source"],
            "weight": r["weight"],
            "wins": r["wins"],
            "losses": r["losses"],
        })
    output_path.write_text(json.dumps(out, indent=2))
    return FederationStats(exported=len(rows))


def import_examples(store, input_path, trust=0.5):
    """Load examples from JSON. Dedup vs existing (text, wizard). New rows
    are source=federated, weight = original_weight * trust."""
    input_path = Path(input_path)
    data = json.loads(input_path.read_text())
    if data.get("version") != 1:
        raise ValueError("unsupported federation format version: " + str(data.get("version")))
    if data.get("embedding_dim") != 768:
        raise ValueError("embedding dim mismatch: file has " + str(data.get("embedding_dim")))
    stats = FederationStats()
    for ex in data.get("examples", []):
        try:
            text = ex["text"]
            wiz = ex["wizard_name"]
            # Dedup: skip if same (text, wizard) pair exists
            existing = store._conn.execute(
                "SELECT id FROM examples WHERE text = ? AND wizard_name = ?",
                (text, wiz),
            ).fetchone()
            if existing:
                stats.skipped_dup += 1
                continue
            emb_bytes = base64.b64decode(ex["embedding_b64"])
            new_weight = float(ex.get("weight", 0.5)) * trust
            store._conn.execute(
                "INSERT INTO examples(text, embedding, wizard_name, source, weight, wins, losses) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text, emb_bytes, wiz, "federated", new_weight, 0, 0),
            )
            stats.imported += 1
        except Exception:
            stats.errors += 1
    store._conn.commit()
    return stats

