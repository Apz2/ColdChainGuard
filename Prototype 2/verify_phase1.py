"""Phase 1 acceptance checks — run after `python run_demo.py --duration 300`."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> None:
    db_path = Path("ccg.db")
    if not db_path.exists():
        print("ccg.db not found — run `python run_demo.py --duration 300` first")
        sys.exit(1)

    conn = sqlite3.connect(db_path)

    count, min_seq, max_seq = conn.execute(
        "SELECT COUNT(*), MIN(seq), MAX(seq) FROM readings WHERE device_id = ?",
        ("NODE-01",),
    ).fetchone()
    print(f"NODE-01: count={count}, min_seq={min_seq}, max_seq={max_seq}")

    rows = conn.execute(
        "SELECT seq, temp_c FROM readings WHERE device_id = ? ORDER BY seq",
        ("NODE-01",),
    ).fetchall()
    print("Temperatures:", rows)

    by_device = conn.execute(
        "SELECT device_id, COUNT(*) FROM readings GROUP BY device_id ORDER BY device_id"
    ).fetchall()
    print("Readings per device:", by_device)

    conn.close()

    ok = count == 10 and min_seq == 1 and max_seq == 10
    if ok:
        print("PASS: sequence check")
    else:
        print("FAIL: expected count=10, min_seq=1, max_seq=10")
        sys.exit(1)


if __name__ == "__main__":
    main()
