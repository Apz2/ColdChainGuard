"""phase 3 tests for cloud.integrity."""

from __future__ import annotations

import hashlib
import hmac
import time

from cloud.integrity import (
    REJECTION_BAD_MAC,
    REJECTION_REPLAY,
    REJECTION_STALE_TS,
    REJECTION_UNKNOWN_DEVICE,
    IntegrityVerifier,
)
from common.canonical import canonical_bytes

_NODE_KEY = "a3f1c9e27b4d8065"
_KEYS = {"NODE-01": _NODE_KEY}


def _sign(payload: dict, key_hex: str = _NODE_KEY) -> str:
    return hmac.new(
        bytes.fromhex(key_hex),
        canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _payload(seq: int, ts: float, device_id: str = "NODE-01") -> dict:
    return {
        "device_id": device_id,
        "consignment_id": "CN-0417",
        "seq": seq,
        "ts": ts,
        "temp_c": 5.23,
        "humidity_pct": 44.1,
        "door_open": False,
        "lat": 6.5854,
        "lon": 79.9607,
    }


def _envelope(payload: dict, key_hex: str = _NODE_KEY) -> dict:
    return {"payload": payload, "mac": _sign(payload, key_hex)}


def test_unknown_device_rejected() -> None:
    verifier = IntegrityVerifier(_KEYS)
    now = time.time()
    payload = _payload(seq=1, ts=now, device_id="NODE-99")
    accepted, reason = verifier.verify(_envelope(payload), now)
    assert accepted is False
    assert reason == REJECTION_UNKNOWN_DEVICE


def test_bad_mac_rejected() -> None:
    verifier = IntegrityVerifier(_KEYS)
    now = time.time()
    payload = _payload(seq=1, ts=now)
    envelope = {"payload": payload, "mac": "00" * 64}
    accepted, reason = verifier.verify(envelope, now)
    assert accepted is False
    assert reason == REJECTION_BAD_MAC


def test_replay_rejected() -> None:
    verifier = IntegrityVerifier(_KEYS)
    now = time.time()
    payload = _payload(seq=1, ts=now)
    envelope = _envelope(payload)
    assert verifier.verify(envelope, now) == (True, None)
    accepted, reason = verifier.verify(envelope, now)
    assert accepted is False
    assert reason == REJECTION_REPLAY


def test_stale_timestamp_rejected() -> None:
    verifier = IntegrityVerifier(_KEYS, accept_window_s=300.0)
    now = time.time()
    payload = _payload(seq=1, ts=now - 600.0)
    accepted, reason = verifier.verify(_envelope(payload), now)
    assert accepted is False
    assert reason == REJECTION_STALE_TS


def test_valid_frame_accepted() -> None:
    verifier = IntegrityVerifier(_KEYS)
    now = time.time()
    payload = _payload(seq=1, ts=now)
    accepted, reason = verifier.verify(_envelope(payload), now)
    assert accepted is True
    assert reason is None


def test_rejection_reasons_are_distinct() -> None:
    """checking each bad frame only hits one rejection reason."""
    verifier = IntegrityVerifier(_KEYS, accept_window_s=300.0)
    now = time.time()

    baseline = _envelope(_payload(seq=1, ts=now))
    assert verifier.verify(baseline, now)[1] is None

    reasons = {
        verifier.verify(baseline, now)[1],
        verifier.verify(
            {"payload": _payload(seq=2, ts=now), "mac": "ab" * 32},
            now,
        )[1],
        verifier.verify(
            _envelope(_payload(seq=3, ts=now - 600.0)),
            now,
        )[1],
        verifier.verify(
            _envelope(_payload(seq=1, ts=now, device_id="NODE-99")),
            now,
        )[1],
    }
    assert reasons == {
        REJECTION_REPLAY,
        REJECTION_BAD_MAC,
        REJECTION_STALE_TS,
        REJECTION_UNKNOWN_DEVICE,
    }
