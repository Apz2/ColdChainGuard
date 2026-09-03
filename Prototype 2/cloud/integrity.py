"""checking signed reading envelopes — figure 3 logic."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3

from common.canonical import canonical_bytes

REJECTION_UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
REJECTION_BAD_MAC = "BAD_MAC"
REJECTION_REPLAY = "REPLAY"
REJECTION_STALE_TS = "STALE_TS"

VALID_REJECTIONS = frozenset(
    {
        REJECTION_UNKNOWN_DEVICE,
        REJECTION_BAD_MAC,
        REJECTION_REPLAY,
        REJECTION_STALE_TS,
    }
)


def load_last_seen_seq(conn: sqlite3.Connection) -> dict[str, int]:
    """loading highest accepted seq per device from the db on startup."""
    rows = conn.execute(
        """
        SELECT device_id, MAX(seq) AS last_seq
        FROM readings
        WHERE verified = 1
        GROUP BY device_id
        """
    ).fetchall()
    last_seen: dict[str, int] = {}
    for row in rows:
        last_seen[row[0]] = int(row[1])
    return last_seen


class IntegrityVerifier:
    """checking signed envelopes — four rejection reasons (figure 3):

      UNKNOWN_DEVICE — device_id not in key store
      BAD_MAC        — HMAC doesn't match
      REPLAY         — seq <= last seen for that device
      STALE_TS       — timestamp too far from now
    """

    def __init__(
        self,
        keys: dict[str, str],
        accept_window_s: float = 300.0,
        last_seen: dict[str, int] | None = None,
    ) -> None:
        self._keys = {device_id: bytes.fromhex(key_hex) for device_id, key_hex in keys.items()}
        self._accept_window_s = accept_window_s
        self._last_seen: dict[str, int] = dict(last_seen or {})

    def verify(self, envelope: dict, now: float) -> tuple[bool, str | None]:
        """returning (True, None) if ok, (False, reason) if rejecting."""
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return False, REJECTION_BAD_MAC

        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or device_id not in self._keys:
            return False, REJECTION_UNKNOWN_DEVICE

        provided_mac = envelope.get("mac")
        if not isinstance(provided_mac, str):
            return False, REJECTION_BAD_MAC

        expected_mac = hmac.new(
            self._keys[device_id],
            canonical_bytes(payload),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(provided_mac, expected_mac):
            return False, REJECTION_BAD_MAC

        seq = payload.get("seq")
        if not isinstance(seq, int):
            return False, REJECTION_REPLAY

        last_seen_seq = self._last_seen.get(device_id, 0)
        if seq <= last_seen_seq:
            return False, REJECTION_REPLAY

        ts = payload.get("ts")
        if not isinstance(ts, (int, float)):
            return False, REJECTION_STALE_TS

        if abs(float(ts) - now) > self._accept_window_s:
            return False, REJECTION_STALE_TS

        self._last_seen[device_id] = seq
        return True, None
