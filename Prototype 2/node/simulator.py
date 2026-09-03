"""fake ESP32 node with DS18B20-ish behaviour."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import signal
import sys
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import yaml

from common.canonical import canonical_bytes
from common.clock import SimClock

logger = logging.getLogger(__name__)

_THERMAL_ALPHA = 0.08
_NOISE_SIGMA_C = 0.15
_SAMPLE_INTERVAL_S = 30.0

# base coords near colombo — from the spec example
_BASE_LAT = 6.5854
_BASE_LON = 79.9607


class SensorNode:
    """simulating a node — target temp drifts toward setpoint with inertia + noise.

    temp_c += (target_c - temp_c) * alpha   (alpha = 0.08)
    then adding gaussian noise sigma 0.15 °C
    """

    def __init__(
        self,
        device_id: str,
        consignment_id: str,
        key: str,
        clock: SimClock,
        rng: np.random.Generator,
    ) -> None:
        self._device_id = device_id
        self._consignment_id = consignment_id
        self._key = bytes.fromhex(key)
        self._clock = clock
        self._rng = rng
        self._target_c = 5.0
        self._temp_c = 5.0
        self._door_open = False
        self._seq = 0
        self._lat = _BASE_LAT + (hash(device_id) % 100) * 0.0001
        self._lon = _BASE_LON + (hash(device_id) % 100) * 0.0001
        self._humidity_pct = 44.0

    def set_target(self, target_c: float) -> None:
        """setting the environmental setpoint."""
        self._target_c = target_c

    def set_door(self, is_open: bool) -> None:
        """toggling door open/closed."""
        self._door_open = is_open

    def read(self) -> dict:
        """taking a reading, signing it, bumping seq."""
        self._temp_c += (self._target_c - self._temp_c) * _THERMAL_ALPHA
        self._temp_c += self._rng.normal(0.0, _NOISE_SIGMA_C)
        self._humidity_pct += self._rng.normal(0.0, 0.3)
        self._humidity_pct = max(20.0, min(80.0, self._humidity_pct))

        self._seq += 1
        payload = {
            "device_id": self._device_id,
            "consignment_id": self._consignment_id,
            "seq": self._seq,
            "ts": self._clock.now(),
            "temp_c": round(self._temp_c, 2),
            "humidity_pct": round(self._humidity_pct, 1),
            "door_open": self._door_open,
            "lat": round(self._lat, 4),
            "lon": round(self._lon, 4),
        }
        mac = hmac.new(
            self._key,
            canonical_bytes(payload),
            hashlib.sha256,
        ).hexdigest()
        return {"payload": payload, "mac": mac}


def _load_device_config(device_id: str, devices_path: Path) -> dict:
    with devices_path.open(encoding="utf-8") as handle:
        devices = yaml.safe_load(handle)
    if device_id not in devices:
        raise ValueError(f"Unknown device_id: {device_id}")
    return devices[device_id]


def run_node(args: argparse.Namespace) -> None:
    devices_path = Path(args.devices)
    device_cfg = _load_device_config(args.device, devices_path)

    clock = SimClock(args.start_epoch, args.compression)
    rng = np.random.default_rng(args.seed)
    node = SensorNode(
        device_id=args.device,
        consignment_id=device_cfg["consignment"],
        key=device_cfg["key"],
        clock=clock,
        rng=rng,
    )

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"node-{args.device}",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(ca_certs=args.ca_cert)
    client.tls_insecure_set(True)
    client.connect(args.broker_host, args.broker_port, keepalive=60)
    client.loop_start()

    running = True

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("Node %s received signal %s, stopping", args.device, signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    topic = f"ccg/node/{args.device}"
    end_ts = args.start_epoch + args.duration
    logger.info(
        "Node %s publishing to %s until sim ts %.1f",
        args.device,
        topic,
        end_ts,
    )

    try:
        while running and clock.now() < end_ts:
            envelope = node.read()
            payload_json = json.dumps(envelope)
            result = client.publish(topic, payload_json, qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(
                    "Publish failed for %s seq=%s rc=%s",
                    args.device,
                    envelope["payload"]["seq"],
                    result.rc,
                )
            else:
                logger.info(
                    "Published %s seq=%s temp_c=%.2f",
                    args.device,
                    envelope["payload"]["seq"],
                    envelope["payload"]["temp_c"],
                )
            clock.sleep_sim(_SAMPLE_INTERVAL_S)
    finally:
        client.loop_stop()
        client.disconnect()
        logger.info("Node %s shut down", args.device)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard sensor node simulator")
    parser.add_argument("--device", required=True, help="Device ID, e.g. NODE-01")
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=8883)
    parser.add_argument("--ca-cert", default="config/certs/ca.crt")
    parser.add_argument("--devices", default="config/devices.yaml")
    parser.add_argument("--start-epoch", type=float, required=True)
    parser.add_argument("--compression", type=float, default=120.0)
    parser.add_argument("--duration", type=float, required=True,
                        help="Simulated run duration in seconds")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    run_node(args)


if __name__ == "__main__":
    main(sys.argv[1:])
