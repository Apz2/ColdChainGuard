"""canonical payload format for HMAC signing — needs to be deterministic."""

from __future__ import annotations


def canonical_bytes(payload: dict) -> bytes:
    """turning a payload dict into the exact bytes i'm HMAC-ing.

    fixed key order: device_id, consignment_id, seq, ts, temp_c,
    humidity_pct, door_open, lat, lon
    floats: ts %.3f, temp_c %.2f, humidity %.1f, lat/lon %.4f
    bools as "true"/"false", joined with |
    """
    door_str = "true" if payload["door_open"] else "false"
    parts = [
        str(payload["device_id"]),
        str(payload["consignment_id"]),
        str(payload["seq"]),
        f"{payload['ts']:.3f}",
        f"{payload['temp_c']:.2f}",
        f"{payload['humidity_pct']:.1f}",
        door_str,
        f"{payload['lat']:.4f}",
        f"{payload['lon']:.4f}",
    ]
    return "|".join(parts).encode("utf-8")
