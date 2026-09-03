"""phase 3 tests for gateway.queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.queue import StoreAndForwardQueue


def _envelope(device_id: str, seq: int) -> dict:
    return {
        "payload": {
            "device_id": device_id,
            "consignment_id": "CN-0417",
            "seq": seq,
            "ts": 1735689600.0 + seq,
            "temp_c": 5.0,
            "humidity_pct": 44.0,
            "door_open": False,
            "lat": 6.5854,
            "lon": 79.9607,
        },
        "mac": "deadbeef",
    }


def test_enqueue_increases_pending_count(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    assert queue.pending_count() == 0
    queue.enqueue(_envelope("NODE-01", 1))
    assert queue.pending_count() == 1
    queue.close()


def test_flush_replays_in_strict_seq_order(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    queue.enqueue(_envelope("NODE-01", 3))
    queue.enqueue(_envelope("NODE-01", 1))
    queue.enqueue(_envelope("NODE-01", 2))

    published: list[int] = []

    def publish_fn(envelope: dict) -> bool:
        published.append(envelope["payload"]["seq"])
        return True

    sent = queue.flush(publish_fn)
    assert sent == 3
    assert published == [1, 2, 3]
    assert queue.pending_count() == 3
    queue.close()


def test_flush_stops_on_first_failure(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    queue.enqueue(_envelope("NODE-01", 1))
    queue.enqueue(_envelope("NODE-01", 2))
    queue.enqueue(_envelope("NODE-01", 3))

    published: list[int] = []

    def publish_fn(envelope: dict) -> bool:
        seq = envelope["payload"]["seq"]
        if seq == 2:
            return False
        published.append(seq)
        return True

    sent = queue.flush(publish_fn)
    assert sent == 1
    assert published == [1]
    assert queue.pending_count() == 3
    queue.close()


def test_acknowledge_removes_delivered_items(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    queue.enqueue(_envelope("NODE-01", 1))
    queue.enqueue(_envelope("NODE-01", 2))
    queue.acknowledge("NODE-01", 1)
    assert queue.pending_count() == 1
    queue.acknowledge("NODE-01", 2)
    assert queue.pending_count() == 0
    queue.close()


def test_queue_survives_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "queue.db")
    queue = StoreAndForwardQueue(db_path)
    queue.enqueue(_envelope("NODE-02", 5))
    queue.close()

    reopened = StoreAndForwardQueue(db_path)
    assert reopened.pending_count() == 1
    published: list[int] = []

    def publish_fn(envelope: dict) -> bool:
        published.append(envelope["payload"]["seq"])
        return True

    reopened.flush(publish_fn)
    assert published == [5]
    reopened.acknowledge("NODE-02", 5)
    assert reopened.pending_count() == 0
    reopened.close()


def test_per_device_ordering_is_independent(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    queue.enqueue(_envelope("NODE-02", 2))
    queue.enqueue(_envelope("NODE-01", 2))
    queue.enqueue(_envelope("NODE-01", 1))
    queue.enqueue(_envelope("NODE-02", 1))

    published: list[tuple[str, int]] = []

    def publish_fn(envelope: dict) -> bool:
        payload = envelope["payload"]
        published.append((payload["device_id"], payload["seq"]))
        return True

    queue.flush(publish_fn)
    assert published == [
        ("NODE-01", 1),
        ("NODE-01", 2),
        ("NODE-02", 1),
        ("NODE-02", 2),
    ]
    queue.close()


def test_duplicate_enqueue_is_ignored(tmp_path: Path) -> None:
    queue = StoreAndForwardQueue(str(tmp_path / "queue.db"))
    queue.enqueue(_envelope("NODE-01", 1))
    queue.enqueue(_envelope("NODE-01", 1))
    assert queue.pending_count() == 1
    queue.close()
