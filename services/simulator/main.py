"""Fleet simulator: replays NASA C-MAPSS trajectories as MQTT telemetry to AWS IoT Core.

Configuration (environment variables; CLI flags override):

    MQTT_HOST            IoT Core data endpoint (xxxx-ats.iot.<region>.amazonaws.com)
    MQTT_PORT            8883 (TLS) or 1883 (plain, for non-AWS brokers)
    MQTT_CLIENT_ID       must match the IoT policy's client-id condition (default: pdm-gateway)
    MQTT_TLS             "true" (default) / "false"
    IOT_CERT_PEM / IOT_PRIVATE_KEY_PEM    certificate & key *contents* (injected by ECS from SSM)
    IOT_CERT_PATH / IOT_KEY_PATH          ...or file paths (local runs)
    IOT_CA_PATH          CA bundle path (default: certifi bundle, which includes Amazon Root CA 1)
    SIM_MACHINES         fleet size (default 10)
    SIM_INTERVAL         seconds per machine cycle (default 5)
    SIM_DATASET/SIM_SPLIT/SIM_SITE/SIM_SEED/SIM_NOISE
    SIM_MAX_TICKS        stop after N ticks (default: run forever)

Run locally against the deployed stack:
    make iot-certs && python services/simulator/main.py --host $(terraform output -raw iot_endpoint)
Dry run (prints JSON instead of publishing):
    python services/simulator/main.py --dry-run --max-ticks 3
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import ssl
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pdm.cmapss import download  # noqa: E402
from pdm.simulator import FleetSimulator  # noqa: E402

log = logging.getLogger("pdm.simulator")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _materialise(pem_env: str, path_env: str, suffix: str) -> str | None:
    """Return a file path for a PEM provided either as contents or as a path."""
    path = _env(path_env)
    if path:
        return path
    pem = _env(pem_env)
    if pem:
        f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
        f.write(pem.replace("\\n", "\n"))
        f.close()
        os.chmod(f.name, 0o600)
        return f.name
    return None


class MqttPublisher:
    def __init__(self, host: str, port: int, client_id: str, tls: bool):
        import paho.mqtt.client as mqtt

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311
        )
        self.client.on_connect = lambda c, u, flags, rc, props=None: log.info("connected rc=%s", rc)
        self.client.on_disconnect = lambda c, u, flags, rc, props=None: log.warning("disconnected rc=%s", rc)
        if tls:
            cert = _materialise("IOT_CERT_PEM", "IOT_CERT_PATH", ".crt")
            key = _materialise("IOT_PRIVATE_KEY_PEM", "IOT_KEY_PATH", ".key")
            ca = _env("IOT_CA_PATH")
            if not ca:
                import certifi

                ca = certifi.where()
            if not (cert and key):
                raise SystemExit(
                    "TLS enabled but no client certificate/key provided (IOT_CERT_PEM/IOT_PRIVATE_KEY_PEM)"
                )
            self.client.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

    def publish(self, topic: str, payload: str) -> None:
        info = self.client.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=10)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class StdoutPublisher:
    def publish(self, topic: str, payload: str) -> None:
        print(f"{topic} {payload}")

    def close(self) -> None:
        pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=_env("MQTT_HOST"))
    ap.add_argument("--port", type=int, default=int(_env("MQTT_PORT", "8883")))
    ap.add_argument("--client-id", default=_env("MQTT_CLIENT_ID", "pdm-gateway"))
    ap.add_argument("--no-tls", action="store_true", default=_env("MQTT_TLS", "true").lower() == "false")
    ap.add_argument("--machines", type=int, default=int(_env("SIM_MACHINES", "10")))
    ap.add_argument("--interval", type=float, default=float(_env("SIM_INTERVAL", "5")))
    ap.add_argument("--dataset", default=_env("SIM_DATASET", "FD001"))
    ap.add_argument("--split", default=_env("SIM_SPLIT", "train"))
    ap.add_argument("--site", default=_env("SIM_SITE", "nagoya"))
    ap.add_argument("--seed", type=int, default=int(_env("SIM_SEED", "42")))
    ap.add_argument("--noise", type=float, default=float(_env("SIM_NOISE", "0")))
    ap.add_argument("--max-ticks", type=int, default=int(_env("SIM_MAX_TICKS", "0")) or None)
    ap.add_argument("--data-dir", default=_env("PDM_DATA_DIR", "data/cmapss"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    download(args.data_dir)
    sim = FleetSimulator(
        n_machines=args.machines,
        dataset=args.dataset,
        split=args.split,
        site=args.site,
        seed=args.seed,
        noise_std=args.noise,
        data_dir=args.data_dir,
    )
    if args.dry_run:
        pub = StdoutPublisher()
    else:
        if not args.host:
            raise SystemExit("--host / MQTT_HOST is required (or use --dry-run)")
        pub = MqttPublisher(args.host, args.port, args.client_id, tls=not args.no_tls)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    log.info(
        "fleet of %d machines, %.1fs per cycle, dataset %s/%s",
        args.machines,
        args.interval,
        args.dataset,
        args.split,
    )
    sent = 0
    try:
        for batch in sim.stream(max_ticks=args.max_ticks):
            t0 = time.time()
            for msg in batch:
                pub.publish(msg.topic, msg.model_dump_json())
                sent += 1
            if sim.tick % 12 == 0:
                log.info("tick %d: %d messages sent; e.g. %s cycle=%d true_rul=%s",
                         sim.tick, sent, batch[0].machine_id, batch[0].cycle, sim.true_rul(batch[0].machine_id))  # fmt: skip
            if stop["flag"]:
                break
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    finally:
        pub.close()
        log.info("stopped after %d messages", sent)


if __name__ == "__main__":
    main()
