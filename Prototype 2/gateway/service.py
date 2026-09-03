"""gateway — forwarding node readings and publishing excursion events."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

from gateway.rules import ExcursionStateMachine

logger = logging.getLogger(__name__)

_EVENT_TYPES = frozenset({"WARNING", "EXCURSION", "RECOVERED"})


class GatewayService:
    """subscribing to nodes, running state machine, forwarding to cloud topics."""

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        ca_cert: str,
        consignments_path: str = "config/consignments.yaml",
        dwell_s: float = 120.0,
        recovery_s: float = 300.0,
    ) -> None:
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._ca_cert = ca_cert
        self._running = True
        self._state_machines: dict[str, ExcursionStateMachine] = {}
        self._load_consignments(consignments_path, dwell_s, recovery_s)
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="gateway",
            protocol=mqtt.MQTTv311,
        )
        self._client.tls_set(ca_certs=ca_cert)
        self._client.tls_insecure_set(True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _load_consignments(
        self,
        consignments_path: str,
        dwell_s: float,
        recovery_s: float,
    ) -> None:
        path = Path(consignments_path)
        with path.open(encoding="utf-8") as handle:
            consignments = yaml.safe_load(handle)

        for consignment_id, details in consignments.items():
            self._state_machines[consignment_id] = ExcursionStateMachine(
                low_c=details["low_limit_c"],
                high_c=details["high_limit_c"],
                dwell_s=dwell_s,
                recovery_s=recovery_s,
            )

    def _get_state_machine(self, consignment_id: str) -> ExcursionStateMachine | None:
        return self._state_machines.get(consignment_id)

    def _publish_state_change(
        self,
        client: mqtt.Client,
        consignment_id: str,
        ts: float,
        state: str,
    ) -> None:
        if state not in _EVENT_TYPES:
            return

        event_topic = f"ccg/events/{consignment_id}"
        event_payload = json.dumps(
            {
                "consignment_id": consignment_id,
                "ts": ts,
                "type": state,
                "state": state,
            }
        )
        result = client.publish(event_topic, event_payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                "Event publish failed for %s state=%s rc=%s",
                consignment_id,
                state,
                result.rc,
            )
            return

        logger.info("Published %s for %s on %s", state, consignment_id, event_topic)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("Gateway connect failed: %s", reason_code)
            return
        client.subscribe("ccg/node/+", qos=1)
        logger.info("Gateway subscribed to ccg/node/+")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            envelope = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Invalid JSON from %s: %s", message.topic, exc)
            return

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            logger.error("Missing payload in message from %s", message.topic)
            return

        consignment_id = payload.get("consignment_id")
        if not consignment_id:
            logger.error("Missing consignment_id in message from %s", message.topic)
            return

        temp_c = payload.get("temp_c")
        ts = payload.get("ts")
        if temp_c is None or ts is None:
            logger.error("Missing temp_c or ts in message from %s", message.topic)
            return

        state_machine = self._get_state_machine(consignment_id)
        if state_machine is not None:
            state_change = state_machine.update(float(temp_c), float(ts))
            if state_change is not None:
                self._publish_state_change(
                    client,
                    consignment_id,
                    float(ts),
                    state_change,
                )

        out_topic = f"ccg/readings/{consignment_id}"
        out_payload = json.dumps(envelope)
        result = client.publish(out_topic, out_payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                "Forward failed for %s seq=%s rc=%s",
                payload.get("device_id"),
                payload.get("seq"),
                result.rc,
            )
            return

        logger.info(
            "Forwarded %s seq=%s -> %s",
            payload.get("device_id"),
            payload.get("seq"),
            out_topic,
        )

    def start(self) -> None:
        self._client.connect(self._broker_host, self._broker_port, keepalive=60)
        self._client.loop_start()
        logger.info("Gateway started on %s:%s", self._broker_host, self._broker_port)

    def stop(self) -> None:
        self._running = False
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("Gateway stopped")

    def wait(self) -> None:
        while self._running:
            signal.pause() if hasattr(signal, "pause") else __import__("time").sleep(0.5)


def run_gateway(args: argparse.Namespace) -> None:
    service = GatewayService(
        args.broker_host,
        args.broker_port,
        args.ca_cert,
        consignments_path=args.consignments,
        dwell_s=args.dwell_s,
        recovery_s=args.recovery_s,
    )

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Gateway received signal %s, stopping", signum)
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    service.start()
    try:
        import time
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        service.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard edge gateway")
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=8883)
    parser.add_argument("--ca-cert", default="config/certs/ca.crt")
    parser.add_argument("--consignments", default="config/consignments.yaml")
    parser.add_argument("--dwell-s", type=float, default=120.0)
    parser.add_argument("--recovery-s", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    run_gateway(parse_args(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
