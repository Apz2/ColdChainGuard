"""Print recent readings for Phase 1 screenshot evidence."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> None:
    db_path = Path("ccg.db")
    if not db_path.exists():
        print("ccg.db not found")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT device_id, seq, ts, temp_c, mac
        FROM readings
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()

    print(f"{'device_id':<10} {'seq':>5} {'ts':>14} {'temp_c':>8}  mac")
    print("-" * 80)
    for device_id, seq, ts, temp_c, mac in rows:
        print(f"{device_id:<10} {seq:>5} {ts:>14.3f} {temp_c:>8.2f}  {mac}")

    total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    print(f"\nTotal rows in readings: {total}")
    conn.close()


if __name__ == "__main__":
    main()
