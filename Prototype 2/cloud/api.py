"""cloud API + MQTT subscriber."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import paho.mqtt.client as mqtt
import uvicorn
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from cloud.integrity import IntegrityVerifier, load_last_seen_seq
from cloud.store import Store, _DEFAULT_INTERVAL_S
from cloud.thermal import degree_minutes, mean_kinetic_temperature

logger = logging.getLogger(__name__)

_store: Store | None = None
_mqtt_client: mqtt.Client | None = None
_verifier: IntegrityVerifier | None = None

VALID_ROLES = frozenset({"operator", "auditor", "supervisor"})
_DEVICE_ONLINE_THRESHOLD_S = 90.0
_SEVERITY_RANK = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
_DISPOSITION_ACTION = {
    "QUARANTINE": "QUARANTINE",
    "INVESTIGATE": "INVESTIGATE",
    "RELEASE": "MONITOR",
}


def redact_location(lat: float, lon: float, role: str) -> tuple[float, float]:
    """rounding GPS to 2 dp unless supervisor — need this for the privacy bit in 5.4."""
    if role == "supervisor":
        return (round(lat, 4), round(lon, 4))
    return (round(lat, 2), round(lon, 2))


def _load_device_keys(devices_path: str) -> dict[str, str]:
    path = Path(devices_path)
    with path.open(encoding="utf-8") as handle:
        devices = yaml.safe_load(handle)
    return {device_id: details["key"] for device_id, details in devices.items()}


def _log_security_event(
    device_id: str | None,
    reason: str,
    envelope: dict,
    event_ts: float,
) -> None:
    if _store is None:
        logger.error("Cannot log security event %s: store not initialised", reason)
        return
    _store._conn.execute(
        """
        INSERT INTO security_events (ts, device_id, reason, raw_payload)
        VALUES (?, ?, ?, ?)
        """,
        (event_ts, device_id, reason, json.dumps(envelope)),
    )
    _store._conn.commit()
    logger.warning(
        "Rejected reading from %s: %s",
        device_id or "unknown",
        reason,
    )


def _dispatch_mqtt_message(
    client: mqtt.Client,
    userdata: object,
    message: mqtt.MQTTMessage,
) -> None:
    if message.topic.startswith("ccg/events/"):
        _on_events_message(client, userdata, message)
        return
    if message.topic.startswith("ccg/readings/"):
        _on_readings_message(client, userdata, message)
        return
    logger.error("Unexpected MQTT topic: %s", message.topic)


def _on_events_message(
    client: mqtt.Client,
    userdata: object,
    message: mqtt.MQTTMessage,
) -> None:
    if _store is None:
        logger.error("Store not initialised; dropping event on %s", message.topic)
        return

    try:
        event = json.loads(message.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Invalid JSON on %s: %s", message.topic, exc)
        return

    consignment_id = event.get("consignment_id")
    event_type = event.get("type")
    ts = event.get("ts")
    if not consignment_id or not event_type or ts is None:
        logger.error("Missing fields in event on %s", message.topic)
        return

    if event_type not in {"WARNING", "EXCURSION", "RECOVERED"}:
        logger.error("Unknown event type %s on %s", event_type, message.topic)
        return

    _store.insert_event(consignment_id, float(ts), event_type)
    _store.recompute_disposition(consignment_id)
    logger.info("Recorded %s event for %s", event_type, consignment_id)


def _on_readings_message(
    client: mqtt.Client,
    userdata: object,
    message: mqtt.MQTTMessage,
) -> None:
    if _store is None or _verifier is None:
        logger.error("Store or verifier not initialised; dropping message on %s", message.topic)
        return

    try:
        envelope = json.loads(message.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Invalid JSON on %s: %s", message.topic, exc)
        return

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        logger.error("Missing payload on %s", message.topic)
        return

    now = time.time()
    accepted, reason = _verifier.verify(envelope, now)
    device_id = payload.get("device_id") if isinstance(payload.get("device_id"), str) else None

    if not accepted:
        _log_security_event(device_id, reason or "BAD_MAC", envelope, now)
        return

    stored = _store.insert_reading(envelope, verified=1)
    if not stored:
        _log_security_event(device_id, "REPLAY", envelope, now)
        return

    seq = payload["seq"]
    ack_topic = f"ccg/ack/{device_id}"
    ack_payload = json.dumps({"device_id": device_id, "seq": seq})
    result = client.publish(ack_topic, ack_payload, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error("Ack publish failed for %s seq=%s rc=%s", device_id, seq, result.rc)
    else:
        logger.info("Acknowledged %s seq=%s on %s", device_id, seq, ack_topic)


def _start_mqtt(broker_host: str, broker_port: int, ca_cert: str) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="cloud",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(ca_certs=ca_cert)
    client.tls_insecure_set(True)

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("Cloud MQTT connect failed: %s", reason_code)
            return
        client.subscribe("ccg/readings/+", qos=1)
        client.subscribe("ccg/events/+", qos=1)
        logger.info("Cloud subscribed to ccg/readings/+ and ccg/events/+")

    client.on_connect = on_connect
    client.on_message = _dispatch_mqtt_message
    client.connect(broker_host, broker_port, keepalive=60)
    client.loop_start()
    return client


def _require_role(
    x_role: Annotated[str | None, Header()] = None,
) -> str:
    if x_role is None:
        raise HTTPException(status_code=401, detail="X-Role header required")
    role = x_role.strip().lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=403, detail=f"Invalid role: {x_role}")
    return role


def _require_auditor(role: Annotated[str, Depends(_require_role)]) -> str:
    if role != "auditor":
        raise HTTPException(status_code=403, detail="Auditor role required")
    return role


def _disposition_to_action(disposition_value: str) -> str:
    return _DISPOSITION_ACTION.get(disposition_value, "MONITOR")


def _compute_thermal(consignment_id: str) -> dict[str, object]:
    if _store is None:
        raise HTTPException(status_code=503, detail="Store not initialised")

    consignment = _store._conn.execute(
        """
        SELECT low_limit_c, high_limit_c, budget_dm, disposition
        FROM consignments
        WHERE id = ?
        """,
        (consignment_id,),
    ).fetchone()
    if consignment is None:
        raise HTTPException(status_code=404, detail=f"Unknown consignment: {consignment_id}")

    readings = _store._conn.execute(
        """
        SELECT ts, temp_c
        FROM readings
        WHERE consignment_id = ? AND verified = 1
        ORDER BY ts ASC, id ASC
        """,
        (consignment_id,),
    ).fetchall()

    low_c = consignment["low_limit_c"]
    high_c = consignment["high_limit_c"]
    budget_dm = consignment["budget_dm"]
    consumed_dm = 0.0
    prev_ts: float | None = None
    temps_c: list[float] = []

    for reading in readings:
        temp_c = reading["temp_c"]
        ts = reading["ts"]
        temps_c.append(temp_c)

        if prev_ts is None:
            interval_s = _DEFAULT_INTERVAL_S
        else:
            interval_s = ts - prev_ts
            if interval_s <= 0.0:
                interval_s = _DEFAULT_INTERVAL_S
        prev_ts = ts

        consumed_dm += degree_minutes(temp_c, low_c, high_c, interval_s)

    mkt_c = mean_kinetic_temperature(temps_c) if temps_c else None
    return {
        "mkt_c": round(mkt_c, 2) if mkt_c is not None else None,
        "consumed_dm": round(consumed_dm, 2),
        "budget_dm": budget_dm,
        "disposition": consignment["disposition"],
    }


def _latest_sim_ts() -> float | None:
    if _store is None:
        return None
    row = _store._conn.execute(
        "SELECT MAX(ts) AS latest_ts FROM readings WHERE verified = 1"
    ).fetchone()
    if row is None or row["latest_ts"] is None:
        return None
    return float(row["latest_ts"])


def _walk_integrity_chain(consignment_id: str) -> dict[str, object]:
    if _store is None:
        raise HTTPException(status_code=503, detail="Store not initialised")

    rows = _store._conn.execute(
        """
        SELECT device_id, seq
        FROM readings
        WHERE consignment_id = ? AND verified = 1
        ORDER BY device_id ASC, seq ASC
        """,
        (consignment_id,),
    ).fetchall()

    gaps: list[dict[str, int]] = []
    last_by_device: dict[str, int] = {}

    for row in rows:
        device_id = row[0]
        seq = int(row[1])
        previous = last_by_device.get(device_id)
        if previous is not None and seq != previous + 1:
            gaps.append(
                {
                    "device_id": device_id,
                    "expected_seq": previous + 1,
                    "actual_seq": seq,
                }
            )
        last_by_device[device_id] = seq

    return {"chain_intact": len(gaps) == 0, "gaps": gaps}


def create_app(
    db_path: str = "ccg.db",
    consignments_path: str = "config/consignments.yaml",
    devices_path: str = "config/devices.yaml",
    broker_host: str = "127.0.0.1",
    broker_port: int = 8883,
    ca_cert: str = "config/certs/ca.crt",
    accept_window_s: float = 300.0,
) -> FastAPI:
    global _store, _mqtt_client, _verifier

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _store, _mqtt_client, _verifier
        _store = Store(db_path, consignments_path)
        keys = _load_device_keys(devices_path)
        last_seen = load_last_seen_seq(_store._conn)
        _verifier = IntegrityVerifier(keys, accept_window_s=accept_window_s, last_seen=last_seen)
        _mqtt_client = _start_mqtt(broker_host, broker_port, ca_cert)
        logger.info("Cloud service started (db=%s)", db_path)
        yield
        if _mqtt_client is not None:
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()
        if _store is not None:
            _store.close()
        logger.info("Cloud service stopped")

    app = FastAPI(title="ColdChainGuard Cloud", lifespan=lifespan)

    dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")

    app.mount(
        "/dashboard",
        StaticFiles(directory=str(dashboard_dir), html=True),
        name="dashboard",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/consignments")
    def list_consignments(
        role: Annotated[str, Depends(_require_role)],
    ) -> list[dict[str, object]]:
        if _store is None:
            raise HTTPException(status_code=503, detail="Store not initialised")

        rows = _store._conn.execute(
            """
            SELECT id, product, status, disposition, doses
            FROM consignments
            ORDER BY id ASC
            """
        ).fetchall()
        return [
            {
                "id": row["id"],
                "product": row["product"],
                "status": row["status"],
                "disposition": row["disposition"],
                "doses": row["doses"],
            }
            for row in rows
        ]

    @app.get("/api/consignments/{consignment_id}/readings")
    def get_readings(
        consignment_id: str,
        role: Annotated[str, Depends(_require_role)],
        since: float | None = Query(default=None),
    ) -> list[dict[str, object]]:
        if _store is None:
            raise HTTPException(status_code=503, detail="Store not initialised")

        if since is None:
            rows = _store._conn.execute(
                """
                SELECT device_id, seq, ts, temp_c, humidity_pct, door_open, lat, lon
                FROM readings
                WHERE consignment_id = ? AND verified = 1
                ORDER BY ts ASC, id ASC
                """,
                (consignment_id,),
            ).fetchall()
        else:
            rows = _store._conn.execute(
                """
                SELECT device_id, seq, ts, temp_c, humidity_pct, door_open, lat, lon
                FROM readings
                WHERE consignment_id = ? AND verified = 1 AND ts >= ?
                ORDER BY ts ASC, id ASC
                """,
                (consignment_id, since),
            ).fetchall()

        readings: list[dict[str, object]] = []
        for row in rows:
            lat = row["lat"]
            lon = row["lon"]
            if lat is not None and lon is not None:
                redacted_lat, redacted_lon = redact_location(float(lat), float(lon), role)
            else:
                redacted_lat, redacted_lon = lat, lon

            readings.append(
                {
                    "device_id": row["device_id"],
                    "seq": row["seq"],
                    "ts": row["ts"],
                    "temp_c": row["temp_c"],
                    "humidity_pct": row["humidity_pct"],
                    "door_open": bool(row["door_open"]),
                    "lat": redacted_lat,
                    "lon": redacted_lon,
                }
            )
        return readings

    @app.get("/api/consignments/{consignment_id}/thermal")
    def get_thermal(
        consignment_id: str,
        role: Annotated[str, Depends(_require_role)],
    ) -> dict[str, object]:
        return _compute_thermal(consignment_id)

    @app.get("/api/alerts")
    def list_alerts(
        role: Annotated[str, Depends(_require_role)],
    ) -> list[dict[str, object]]:
        if _store is None:
            raise HTTPException(status_code=503, detail="Store not initialised")

        consignments = _store._conn.execute(
            "SELECT id, disposition FROM consignments"
        ).fetchall()
        disposition_by_id = {row["id"]: row["disposition"] for row in consignments}

        events = _store._conn.execute(
            """
            SELECT consignment_id, ts, type, severity, detail
            FROM events
            WHERE type IN ('WARNING', 'EXCURSION')
            ORDER BY ts DESC, id DESC
            """
        ).fetchall()

        open_by_consignment: dict[str, sqlite3.Row] = {}
        for event in events:
            consignment_id = event["consignment_id"]
            if consignment_id in open_by_consignment:
                continue

            recovered = _store._conn.execute(
                """
                SELECT 1 FROM events
                WHERE consignment_id = ? AND type = 'RECOVERED' AND ts > ?
                LIMIT 1
                """,
                (consignment_id, event["ts"]),
            ).fetchone()
            if recovered is not None:
                continue

            open_by_consignment[consignment_id] = event

        alerts: list[dict[str, object]] = []
        for consignment_id, event in open_by_consignment.items():
            disposition_value = disposition_by_id.get(consignment_id, "RELEASE")
            event_type = event["type"]
            if event_type == "EXCURSION":
                description = (
                    event["detail"]
                    or "Sustained temperature excursion outside 2–8 °C limits"
                )
            else:
                description = (
                    event["detail"]
                    or "Temperature warning — breach detected, dwell timer running"
                )

            alerts.append(
                {
                    "consignment_id": consignment_id,
                    "severity": event["severity"],
                    "description": description,
                    "action": _disposition_to_action(disposition_value),
                    "ts": event["ts"],
                    "type": event_type,
                }
            )

        alerts.sort(
            key=lambda item: (
                _SEVERITY_RANK.get(str(item["severity"]), 9),
                -float(item["ts"]),
            )
        )
        return alerts

    @app.get("/api/devices")
    def list_devices(
        role: Annotated[str, Depends(_require_role)],
    ) -> list[dict[str, object]]:
        if _store is None:
            raise HTTPException(status_code=503, detail="Store not initialised")

        latest_ts = _latest_sim_ts()
        rows = _store._conn.execute(
            """
            SELECT device_id, MAX(ts) AS last_seen
            FROM readings
            WHERE verified = 1
            GROUP BY device_id
            ORDER BY device_id ASC
            """
        ).fetchall()

        devices: list[dict[str, object]] = []
        for row in rows:
            last_seen = float(row["last_seen"])
            online = False
            if latest_ts is not None:
                online = (latest_ts - last_seen) <= _DEVICE_ONLINE_THRESHOLD_S
            devices.append(
                {
                    "device_id": row["device_id"],
                    "last_seen": last_seen,
                    "online": online,
                }
            )
        return devices

    @app.get("/api/integrity/{consignment_id}")
    def get_integrity(
        consignment_id: str,
        role: Annotated[str, Depends(_require_auditor)],
    ) -> dict[str, object]:
        return _walk_integrity_chain(consignment_id)

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard cloud service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="ccg.db")
    parser.add_argument("--consignments", default="config/consignments.yaml")
    parser.add_argument("--devices", default="config/devices.yaml")
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=8883)
    parser.add_argument("--ca-cert", default="config/certs/ca.crt")
    parser.add_argument("--accept-window-s", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    app = create_app(
        db_path=args.db,
        consignments_path=args.consignments,
        devices_path=args.devices,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        ca_cert=args.ca_cert,
        accept_window_s=args.accept_window_s,
    )

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Cloud received signal %s, stopping", signum)
        server.should_exit = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    server.run()


if __name__ == "__main__":
    main(sys.argv[1:])
