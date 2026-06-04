#!/usr/bin/env python3.12
"""
Show health stats for swiszard routing DB.

Usage:
  ./health.py              # summary by handler
  ./health.py --details    # list all examples with stats
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from swiszard.db import get_connection


def show_summary():
    """Print per-handler summary stats."""
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT 
                handler,
                COUNT(*) as total,
                SUM(success_count) as total_success,
                SUM(fail_count) as total_fail,
                AVG(success_count) as avg_success,
                AVG(fail_count) as avg_fail,
                MIN(created_at) as oldest,
                MAX(created_at) as newest
            FROM examples
            GROUP BY handler
            ORDER BY total DESC
        """)
        
        print("HANDLER                    EXAMPLES  SUCCESS  FAIL  AVG_S  AVG_F  OLDEST      NEWEST")
        print("-" * 90)
        
        for row in cursor.fetchall():
            handler, total, total_s, total_f, avg_s, avg_f, oldest, newest = row
            handler_short = handler.replace("handler_", "")
            oldest_date = oldest[:10] if oldest else "—"
            newest_date = newest[:10] if newest else "—"
            
            # Warn if success rate < 50%
            warn = ""
            if total_s + total_f > 0 and total_s / (total_s + total_f) < 0.5:
                warn = " ⚠️"
            
            print(f"{handler_short:24} {total:6}  {total_s:7}  {total_f:5}  {avg_s:5.1f}  {avg_f:5.1f}  {oldest_date}  {newest_date}{warn}")
        
        cursor = conn.execute("SELECT COUNT(*) FROM examples")
        grand_total = cursor.fetchone()[0]
        print(f"\nTotal examples: {grand_total}")


def show_details():
    """Print all examples with stats."""
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT handler, phrasing, success_count, fail_count, created_at
            FROM examples
            ORDER BY handler, success_count DESC, phrasing
        """)
        
        current_handler = None
        for row in cursor.fetchall():
            handler, phrasing, success, fail, created = row
            
            if handler != current_handler:
                print(f"\n{handler}:")
                print("-" * 80)
                current_handler = handler
            
            rate = f"{success}✓ {fail}✗"
            created_date = created[:10] if created else "—"
            print(f"  [{rate:8}] {created_date}  {phrasing[:65]}")


def main():
    parser = argparse.ArgumentParser(description="Show swiszard routing DB health")
    parser.add_argument("--details", action="store_true", help="Show all examples")
    args = parser.parse_args()
    
    if args.details:
        show_details()
    else:
        show_summary()


if __name__ == "__main__":
    main()
