"""spinning up the full phase 1 demo — broker, cloud, gateway, nodes."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CERTS_DIR = Path("config/certs")
CA_CERT = CERTS_DIR / "ca.crt"
SERVER_CERT = CERTS_DIR / "server.crt"
SERVER_KEY = CERTS_DIR / "server.key"

NODE_IDS = ("NODE-01", "NODE-02", "NODE-03", "NODE-04", "NODE-05")


def _find_openssl() -> str | None:
    """hunting for openssl on PATH or the usual git-for-windows install."""
    candidates = [
        "openssl",
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ]
    for candidate in candidates:
        if candidate == "openssl":
            import shutil
            found = shutil.which("openssl")
            if found:
                return found
            continue
        if Path(candidate).exists():
            return candidate
    return None


def ensure_certs() -> None:
    """generating self-signed TLS certs for local mqtt if they're missing."""
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    if CA_CERT.exists() and SERVER_CERT.exists() and SERVER_KEY.exists():
        return

    openssl = _find_openssl()
    if openssl is None:
        raise RuntimeError(
            "OpenSSL not found. Install OpenSSL or Git for Windows, "
            "or place ca.crt, server.crt and server.key in config/certs/"
        )

    logger.info("Generating self-signed TLS certificates in %s", CERTS_DIR)
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(SERVER_KEY),
            "-out", str(SERVER_CERT),
            "-days", "365", "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True,
    )
    CA_CERT.write_bytes(SERVER_CERT.read_bytes())
    logger.info("Certificates ready")


async def _run_broker(bind_host: str, bind_port: int) -> None:
    from amqtt.broker import Broker

    config = {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"{bind_host}:{bind_port}",
                "ssl": True,
                "certfile": str(SERVER_CERT),
                "keyfile": str(SERVER_KEY),
            },
        },
        "sys_interval": 10,
    }
    broker = Broker(config)
    await broker.start()
    logger.info("MQTT broker listening on %s:%s (TLS)", bind_host, bind_port)
    stop_event = asyncio.Event()

    def request_stop(signum: int, frame: object) -> None:
        logger.info("Broker received signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        await stop_event.wait()
    finally:
        await broker.stop()
        logger.info("Broker stopped")


def run_broker_mode(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(_run_broker(args.broker_host, args.broker_port))


def _shutdown_processes(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()

    deadline = time.time() + 5.0
    for proc in processes:
        if proc.poll() is not None:
            continue
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            logger.warning("Force-killing pid %s", proc.pid)
            proc.kill()
            proc.wait()


def run_demo(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.db.exists():
        logger.info("Removing existing database %s", args.db)
        args.db.unlink()

    ensure_certs()
    start_epoch = time.time()
    processes: list[subprocess.Popen] = []

    python = sys.executable
    common_args = [
        "--broker-host", args.broker_host,
        "--broker-port", str(args.broker_port),
        "--ca-cert", str(CA_CERT),
    ]

    try:
        broker_cmd = [
            python, str(Path(__file__).resolve()),
            "--broker",
            "--broker-host", args.broker_host,
            "--broker-port", str(args.broker_port),
        ]
        processes.append(subprocess.Popen(broker_cmd))
        time.sleep(1.5)

        cloud_cmd = [
            python, "-m", "cloud.api",
            *common_args,
            "--db", str(args.db),
        ]
        processes.append(subprocess.Popen(cloud_cmd))
        time.sleep(1.0)

        gateway_cmd = [python, "-m", "gateway.service", *common_args]
        processes.append(subprocess.Popen(gateway_cmd))
        time.sleep(0.5)

        node_processes: list[subprocess.Popen] = []
        for index, device_id in enumerate(NODE_IDS):
            node_cmd = [
                python, "-m", "node.simulator",
                "--device", device_id,
                *common_args,
                "--start-epoch", str(start_epoch),
                "--compression", str(args.compression),
                "--duration", str(args.duration),
                "--seed", str(42 + index),
            ]
            node_processes.append(subprocess.Popen(node_cmd))
        processes.extend(node_processes)

        logger.info(
            "Demo running for %s simulated seconds (compression=%s)",
            args.duration,
            args.compression,
        )

        real_timeout = args.duration / args.compression + 15.0
        for proc in node_processes:
            proc.wait(timeout=real_timeout)

        logger.info("All node simulators finished")
    except KeyboardInterrupt:
        logger.info("Ctrl+C received, shutting down")
    finally:
        _shutdown_processes(processes)
        logger.info("All processes stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard demo orchestrator")
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Simulated run duration in seconds (default: 300)",
    )
    parser.add_argument(
        "--compression",
        type=float,
        default=120.0,
        help="Simulated seconds per real second (default: 120)",
    )
    parser.add_argument("--db", type=Path, default=Path("ccg.db"))
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=8883)
    parser.add_argument(
        "--broker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.broker:
        run_broker_mode(args)
    else:
        run_demo(args)


if __name__ == "__main__":
    main(sys.argv[1:])
