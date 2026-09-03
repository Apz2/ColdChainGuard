"""sqlite storage for consignments, readings, events."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import yaml

from cloud.thermal import degree_minutes, disposition

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 30.0

_EVENT_SEVERITY = {
    "WARNING": "WARN",
    "EXCURSION": "CRITICAL",
    "RECOVERED": "INFO",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consignments (
    id            TEXT PRIMARY KEY,
    product       TEXT NOT NULL,
    low_limit_c   REAL NOT NULL,
    high_limit_c  REAL NOT NULL,
    doses         INTEGER NOT NULL,
    budget_dm     REAL NOT NULL,
    status        TEXT NOT NULL,
    disposition   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL,
    consignment_id TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    ts             REAL NOT NULL,
    temp_c         REAL NOT NULL,
    humidity_pct   REAL NOT NULL,
    door_open      INTEGER NOT NULL,
    lat            REAL,
    lon            REAL,
    mac            TEXT NOT NULL,
    verified       INTEGER NOT NULL,
    UNIQUE(device_id, seq)
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    consignment_id TEXT NOT NULL,
    ts             REAL NOT NULL,
    type           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    detail         TEXT
);

CREATE TABLE IF NOT EXISTS security_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    device_id   TEXT,
    reason      TEXT NOT NULL,
    raw_payload TEXT
);
"""


class Store:
    """sqlite backend for cloud data."""

    def __init__(self, db_path: str, consignments_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_consignments(consignments_path)

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _seed_consignments(self, consignments_path: str) -> None:
        path = Path(consignments_path)
        with path.open(encoding="utf-8") as handle:
            consignments = yaml.safe_load(handle)

        for consignment_id, details in consignments.items():
            existing = self._conn.execute(
                "SELECT id FROM consignments WHERE id = ?",
                (consignment_id,),
            ).fetchone()
            if existing is not None:
                continue

            self._conn.execute(
                """
                INSERT INTO consignments (
                    id, product, low_limit_c, high_limit_c,
                    doses, budget_dm, status, disposition
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consignment_id,
                    details["product"],
                    details["low_limit_c"],
                    details["high_limit_c"],
                    details["doses"],
                    details["budget_dm"],
                    "ACTIVE",
                    "RELEASE",
                ),
            )
            logger.info("Seeded consignment %s", consignment_id)

        self._conn.commit()

    def insert_event(
        self,
        consignment_id: str,
        ts: float,
        event_type: str,
        detail: str | None = None,
    ) -> None:
        """writing an excursion state-change event."""
        severity = _EVENT_SEVERITY.get(event_type, "INFO")
        self._conn.execute(
            """
            INSERT INTO events (consignment_id, ts, type, severity, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (consignment_id, ts, event_type, severity, detail),
        )
        self._conn.commit()

    def is_excursion_active(self, consignment_id: str) -> bool:
        """checking if the latest event for this consignment is still EXCURSION."""
        row = self._conn.execute(
            """
            SELECT type FROM events
            WHERE consignment_id = ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (consignment_id,),
        ).fetchone()
        if row is None:
            return False
        return row["type"] == "EXCURSION"

    def recompute_disposition(self, consignment_id: str) -> str:
        """reworking thermal exposure and updating disposition."""
        consignment = self._conn.execute(
            """
            SELECT low_limit_c, high_limit_c, budget_dm
            FROM consignments
            WHERE id = ?
            """,
            (consignment_id,),
        ).fetchone()
        if consignment is None:
            logger.error("Unknown consignment %s for disposition recompute", consignment_id)
            return "RELEASE"

        readings = self._conn.execute(
            """
            SELECT ts, temp_c
            FROM readings
            WHERE consignment_id = ?
            ORDER BY ts ASC, id ASC
            """,
            (consignment_id,),
        ).fetchall()

        low_c = consignment["low_limit_c"]
        high_c = consignment["high_limit_c"]
        budget_dm = consignment["budget_dm"]

        consumed_dm = 0.0
        peak_deviation_c = 0.0
        prev_ts: float | None = None
        temps_c: list[float] = []

        for reading in readings:
            temp_c = reading["temp_c"]
            ts = reading["ts"]
            temps_c.append(temp_c)

            if temp_c < low_c:
                peak_deviation_c = max(peak_deviation_c, low_c - temp_c)
            elif temp_c > high_c:
                peak_deviation_c = max(peak_deviation_c, temp_c - high_c)

            if prev_ts is None:
                interval_s = _DEFAULT_INTERVAL_S
            else:
                interval_s = ts - prev_ts
                if interval_s <= 0.0:
                    interval_s = _DEFAULT_INTERVAL_S
            prev_ts = ts

            consumed_dm += degree_minutes(temp_c, low_c, high_c, interval_s)

        active_excursion = self.is_excursion_active(consignment_id)
        new_disposition = disposition(
            consumed_dm,
            budget_dm,
            active_excursion,
            peak_deviation_c,
        )

        self._conn.execute(
            "UPDATE consignments SET disposition = ? WHERE id = ?",
            (new_disposition, consignment_id),
        )
        self._conn.commit()
        return new_disposition

    def insert_reading(self, envelope: dict, verified: int = 0) -> bool:
        """storing a signed reading — returns False if seq already exists."""
        payload = envelope["payload"]
        try:
            self._conn.execute(
                """
                INSERT INTO readings (
                    device_id, consignment_id, seq, ts, temp_c, humidity_pct,
                    door_open, lat, lon, mac, verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["device_id"],
                    payload["consignment_id"],
                    payload["seq"],
                    payload["ts"],
                    payload["temp_c"],
                    payload["humidity_pct"],
                    1 if payload["door_open"] else 0,
                    payload["lat"],
                    payload["lon"],
                    envelope["mac"],
                    verified,
                ),
            )
            self._conn.commit()
            self.recompute_disposition(payload["consignment_id"])
            return True
        except sqlite3.IntegrityError:
            logger.warning(
                "Duplicate reading rejected for %s seq=%s",
                payload["device_id"],
                payload["seq"],
            )
            return False

    def close(self) -> None:
        self._conn.close()
