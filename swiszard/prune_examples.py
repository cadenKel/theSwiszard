#!/usr/bin/env python3.12
"""
Prune low-quality examples from swiszard routes DB.

Run weekly via cron to:
  - Delete examples with fail_count > success_count * 2 (older than 30 days)
  - Delete deprecated handler examples
  - Deduplicate near-identical phrasings (sim > 0.95)
"""
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

# Add swiszard to path
sys.path.insert(0, str(Path(__file__).parent))

from swiszard.db import get_connection
from swiszard.embeddings import blob_to_array, cosine_similarity

ROUTES_DB = Path.home() / ".hermes" / "swiszard" / "routes.db"
DEPRECATED_HANDLERS = {"handler_llm_fallback"}
FAIL_THRESHOLD = 2  # delete if fail > success * this
MIN_AGE_DAYS = 30
DEDUP_THRESHOLD = 0.95


def prune_deprecated(conn):
    """Delete examples for handlers that no longer exist."""
    placeholders = ",".join("?" * len(DEPRECATED_HANDLERS))
    cursor = conn.execute(
        f"DELETE FROM examples WHERE handler IN ({placeholders})",
        tuple(DEPRECATED_HANDLERS)
    )
    deleted = cursor.rowcount
    conn.commit()
    return deleted


def prune_low_performers(conn):
    """Delete examples with high fail rate after 30-day grace period."""
    cutoff = datetime.now() - timedelta(days=MIN_AGE_DAYS)
    cursor = conn.execute(
        """
        DELETE FROM examples 
        WHERE fail_count > success_count * ?
          AND created_at < ?
        """,
        (FAIL_THRESHOLD, cutoff.isoformat())
    )
    deleted = cursor.rowcount
    conn.commit()
    return deleted


def deduplicate_examples(conn):
    """Remove near-duplicate examples, keeping the one with highest success_count."""
    cursor = conn.execute("SELECT id, handler, phrasing, embedding, success_count FROM examples ORDER BY handler, success_count DESC")
    rows = cursor.fetchall()
    
    deleted = 0
    seen_by_handler = {}
    
    for row in rows:
        row_id, handler, phrasing, embedding_blob, success_count = row
        vec = blob_to_array(embedding_blob)
        
        if handler not in seen_by_handler:
            seen_by_handler[handler] = []
        
        # Check if this embedding is too similar to any we've already kept
        is_duplicate = False
        for kept_id, kept_vec, kept_phrasing in seen_by_handler[handler]:
            sim = cosine_similarity(vec, kept_vec)
            if sim > DEDUP_THRESHOLD:
                # This is a duplicate — delete it
                print(f"  duplicate: '{phrasing[:50]}' ~= '{kept_phrasing[:50]}' (sim={sim:.3f})")
                conn.execute("DELETE FROM examples WHERE id = ?", (row_id,))
                deleted += 1
                is_duplicate = True
                break
        
        if not is_duplicate:
            seen_by_handler[handler].append((row_id, vec, phrasing))
    
    conn.commit()
    return deleted


def main():
    print(f"Pruning swiszard examples DB: {ROUTES_DB}")
    
    with get_connection() as conn:
        deprecated = prune_deprecated(conn)
        print(f"✓ Removed {deprecated} examples for deprecated handlers")
        
        low_performers = prune_low_performers(conn)
        print(f"✓ Removed {low_performers} low-performing examples (fail > {FAIL_THRESHOLD}x success, age > {MIN_AGE_DAYS}d)")
        
        print("Deduplicating examples (sim > {DEDUP_THRESHOLD})...")
        dupes = deduplicate_examples(conn)
        print(f"✓ Removed {dupes} near-duplicate examples")
        
        cursor = conn.execute("SELECT COUNT(*) FROM examples")
        total = cursor.fetchone()[0]
        print(f"\nTotal examples remaining: {total}")


if __name__ == "__main__":
    main()
