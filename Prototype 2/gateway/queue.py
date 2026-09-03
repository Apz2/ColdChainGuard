"""local sqlite queue for readings waiting to reach the cloud."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_readings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    envelope   TEXT NOT NULL,
    UNIQUE(device_id, seq)
);
"""


class StoreAndForwardQueue:
    """queueing readings locally until the cloud acks them.

    using a separate sqlite file so stuff survives gateway restarts.
    flushing in strict seq order per device — sending out of order
    would trip the cloud's replay check.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def enqueue(self, envelope: dict) -> None:
        """stashing a signed envelope for later."""
        payload = envelope["payload"]
        device_id = payload["device_id"]
        seq = payload["seq"]
        envelope_json = json.dumps(envelope)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO pending_readings (device_id, seq, envelope)
            VALUES (?, ?, ?)
            """,
            (device_id, seq, envelope_json),
        )
        self._conn.commit()

    def pending_count(self) -> int:
        """counting how many readings are still waiting to go out."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM pending_readings"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def flush(self, publish_fn: Callable[[dict], bool]) -> int:
        """publishing pending items in seq order — stops on first failure.

        items stay queued until acknowledge() confirms cloud got them.
        """
        rows = self._conn.execute(
            """
            SELECT envelope
            FROM pending_readings
            ORDER BY device_id ASC, seq ASC
            """
        ).fetchall()

        sent = 0
        for envelope_json, in rows:
            envelope = json.loads(envelope_json)
            if not publish_fn(envelope):
                break
            sent += 1
        return sent

    def acknowledge(self, device_id: str, seq: int) -> None:
        """clearing everything up to seq for a device once cloud acked."""
        self._conn.execute(
            """
            DELETE FROM pending_readings
            WHERE device_id = ? AND seq <= ?
            """,
            (device_id, seq),
        )
        self._conn.commit()

    def close(self) -> None:
        """closing the queue db."""
        self._conn.close()
