"""sending four bad frames — one for each rejection reason."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import paho.mqtt.client as mqtt
import yaml

from common.canonical import canonical_bytes

logger = logging.getLogger(__name__)

_CONSIGNMENT_ID = "CN-0417"
_READINGS_TOPIC = f"ccg/readings/{_CONSIGNMENT_ID}"
_STALE_OFFSET_S = 600.0


def _load_keys(devices_path: str) -> dict[str, str]:
    with Path(devices_path).open(encoding="utf-8") as handle:
        devices = yaml.safe_load(handle)
    return {device_id: details["key"] for device_id, details in devices.items()}


def _sign(payload: dict, key_hex: str) -> str:
    return hmac.new(
        bytes.fromhex(key_hex),
        canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _base_payload(device_id: str, seq: int, ts: float) -> dict:
    return {
        "device_id": device_id,
        "consignment_id": _CONSIGNMENT_ID,
        "seq": seq,
        "ts": ts,
        "temp_c": 5.23,
        "humidity_pct": 44.1,
        "door_open": False,
        "lat": 6.5854,
        "lon": 79.9607,
    }


def _publish(
    client: mqtt.Client,
    envelope: dict,
    label: str,
) -> None:
    payload = json.dumps(envelope)
    result = client.publish(_READINGS_TOPIC, payload, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"Publish failed for {label}: rc={result.rc}")
    result.wait_for_publish(timeout=5.0)
    logger.info("Sent %s frame: device=%s seq=%s", label, envelope["payload"]["device_id"], envelope["payload"]["seq"])


def _next_seq(db_path: str, device_id: str) -> int:
    if not Path(db_path).exists():
        return 1
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(seq) FROM readings WHERE device_id = ? AND verified = 1",
            (device_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return 1
    return int(row[0]) + 1


def run_attack(
    broker_host: str,
    broker_port: int,
    ca_cert: str,
    devices_path: str,
    db_path: str,
) -> None:
    keys = _load_keys(devices_path)
    node_key = keys["NODE-01"]
    now = time.time()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="attack",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(ca_certs=ca_cert)
    client.tls_insecure_set(True)
    client.connect(broker_host, broker_port, keepalive=60)
    client.loop_start()
    time.sleep(0.5)

    try:
        accepted_seq = _next_seq(db_path, "NODE-01")
        accepted_payload = _base_payload("NODE-01", accepted_seq, now)
        accepted_envelope = {
            "payload": accepted_payload,
            "mac": _sign(accepted_payload, node_key),
        }
        _publish(client, accepted_envelope, "baseline-accepted")
        time.sleep(0.5)

        _publish(client, accepted_envelope, "REPLAY")
        time.sleep(0.5)

        bad_mac_seq = accepted_seq + 1
        bad_mac_payload = _base_payload("NODE-01", bad_mac_seq, now)
        bad_mac_envelope = {
            "payload": bad_mac_payload,
            "mac": _sign(bad_mac_payload, "ffffffffffffffff"),
        }
        _publish(client, bad_mac_envelope, "BAD_MAC")
        time.sleep(0.5)

        stale_seq = accepted_seq + 2
        stale_payload = _base_payload("NODE-01", stale_seq, now - _STALE_OFFSET_S)
        stale_envelope = {
            "payload": stale_payload,
            "mac": _sign(stale_payload, node_key),
        }
        _publish(client, stale_envelope, "STALE_TS")
        time.sleep(0.5)

        unknown_payload = _base_payload("NODE-99", 1, now)
        unknown_envelope = {
            "payload": unknown_payload,
            "mac": _sign(unknown_payload, node_key),
        }
        _publish(client, unknown_envelope, "UNKNOWN_DEVICE")
        time.sleep(0.5)
    finally:
        client.loop_stop()
        client.disconnect()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard adversarial frame sender")
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=8883)
    parser.add_argument("--ca-cert", default="config/certs/ca.crt")
    parser.add_argument("--devices", default="config/devices.yaml")
    parser.add_argument("--db", default="ccg.db")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    run_attack(
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        ca_cert=args.ca_cert,
        devices_path=args.devices,
        db_path=args.db,
    )
    logger.info("Attack frames sent. Verify with:")
    logger.info('  sqlite3 %s "SELECT reason, COUNT(*) FROM security_events GROUP BY reason;"', args.db)
    logger.info('  sqlite3 %s "SELECT COUNT(*) FROM readings WHERE verified=0;"', args.db)


if __name__ == "__main__":
    main(sys.argv[1:])
