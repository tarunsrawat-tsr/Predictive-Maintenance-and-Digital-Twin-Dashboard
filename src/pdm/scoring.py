"""Streaming scorer: the core of the ingestion pipeline.

Given a micro-batch of telemetry messages (from Kinesis in production, from the in-process
simulator in demo mode) it:

1. groups messages by machine and fetches each machine's recent history from the store to
   rebuild the rolling feature window (stateless Lambda + stateful features),
2. predicts RUL (p10/p50/p90) and the anomaly score for every message,
3. assesses health/status and raises de-duplicated alerts on status transitions,
4. persists enriched telemetry, the digital-twin snapshot, alerts, and optionally fans out to
   Firehose (S3 data lake) and SNS (critical notifications).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np

from pdm.config import Settings
from pdm.health import assess, maintenance_due, recovered, worsened
from pdm.model import ModelBundle
from pdm.schema import TelemetryMessage
from pdm.storage.base import TelemetryStore, raw_vector_from_item

log = logging.getLogger("pdm.scoring")

Sink = Callable[[list[dict]], None]


class StreamScorer:
    def __init__(
        self,
        model: ModelBundle,
        store: TelemetryStore,
        settings: Settings,
        archive_sink: Sink | None = None,
        notify_sink: Sink | None = None,
    ):
        self.model = model
        self.store = store
        self.settings = settings
        self.archive_sink = archive_sink
        self.notify_sink = notify_sink
        self.th = settings.thresholds
        self.econ = settings.economics

    # ------------------------------------------------------------------ public
    def process(self, messages: list[TelemetryMessage]) -> dict:
        """Score a micro-batch. Returns a small summary dict (for Lambda logs/tests)."""
        by_machine: dict[str, list[TelemetryMessage]] = defaultdict(list)
        for m in messages:
            by_machine[m.machine_id].append(m)

        enriched: list[dict] = []
        alerts: list[dict] = []
        for machine_id, msgs in by_machine.items():
            msgs.sort(key=lambda m: (m.ts_ms, m.cycle))
            items, machine_alerts = self._score_machine(machine_id, msgs)
            enriched.extend(items)
            alerts.extend(machine_alerts)

        if enriched:
            self.store.put_telemetry_batch(enriched)
            if self.archive_sink:
                self.archive_sink(enriched)
        for a in alerts:
            self.store.put_alert(a)
        critical = [a for a in alerts if a["severity"] == "critical"]
        if critical and self.notify_sink:
            self.notify_sink(critical)
        return {"messages": len(messages), "machines": len(by_machine), "alerts": len(alerts)}

    # ------------------------------------------------------------------ internals
    def _score_machine(self, machine_id: str, msgs: list[TelemetryMessage]) -> tuple[list[dict], list[dict]]:
        W = self.model.window
        prev_state = self.store.get_machine_state(machine_id)
        history = self.store.get_telemetry(machine_id, limit=W - 1)
        # Guard against overhauls: history from a previous life (cycle > current) must not leak.
        first_cycle = msgs[0].cycle
        history = [h for h in history if int(h.get("cycle", 0)) < first_cycle]
        window = [raw_vector_from_item(h) for h in history]
        prev_status = prev_state.get("status") if prev_state else None
        prev_cycle = int(prev_state.get("cycle", 0)) if prev_state else None
        ewma = float(prev_state.get("anomaly_score", 0.0)) if prev_state else 0.0
        alpha = self.th.anomaly_ewma_alpha

        items: list[dict] = []
        alerts: list[dict] = []
        last_pred = None
        last_assessment = None
        for msg in msgs:
            if msg.cycle == 1 or (prev_cycle is not None and msg.cycle < prev_cycle):
                window = []  # machine was overhauled/reset -> new life
                ewma = 0.0
                prev_status = None
            window.append(msg.raw_vector())
            window = window[-W:]
            pred = self.model.predict_window(np.array(window), float(msg.cycle))
            raw_anomaly = pred.anomaly_score
            if pred.anomaly_ready:
                # EWMA control chart, seeded from the healthy baseline after (re)commissioning.
                prior = ewma if ewma > 0.0 else self.model.anomaly.baseline_score
                ewma = alpha * raw_anomaly + (1 - alpha) * prior
                pred.anomaly_score = ewma
            a = assess(pred.rul_p50, pred.anomaly_score, self.th, previous=prev_status)
            item = {
                "machine_id": msg.machine_id,
                "ts": msg.ts_ms,
                "ts_iso": msg.ts,
                "site": msg.site,
                "line": msg.line,
                "cycle": msg.cycle,
                "settings": msg.settings,
                "sensors": msg.sensors,
                "rul_p10": round(pred.rul_p10, 2),
                "rul_p50": round(pred.rul_p50, 2),
                "rul_p90": round(pred.rul_p90, 2),
                "anomaly_score": round(pred.anomaly_score, 4),
                "anomaly_raw": round(raw_anomaly, 4),
                "anomaly_ready": pred.anomaly_ready,
                "health_index": a.health_index,
                "status": a.status,
                "driver": a.driver,
            }
            items.append(item)

            if worsened(prev_status, a.status):
                alerts.append(self._make_alert(item, a.status, a.reasons, a.driver))
            elif recovered(prev_status, a.status) and a.status == "healthy":
                alerts.append(self._make_alert(item, "info", ["Machine returned to healthy state"]))
            prev_status = a.status
            prev_cycle = msg.cycle
            last_pred, last_assessment = pred, a

        assert last_pred is not None and last_assessment is not None
        last = msgs[-1]
        now = datetime.now(UTC)
        due, cycles_left = maintenance_due(now, last_pred.rul_p10, self.th, self.econ)
        overhauls = int(prev_state.get("overhauls", 0)) if prev_state else 0
        if prev_state and last.cycle < int(prev_state.get("cycle", 0)):
            overhauls += 1
        state = {
            "machine_id": machine_id,
            "site": last.site,
            "line": last.line,
            "last_ts": last.ts_ms,
            "last_ts_iso": last.ts,
            "cycle": last.cycle,
            "rul_p10": round(last_pred.rul_p10, 2),
            "rul_p50": round(last_pred.rul_p50, 2),
            "rul_p90": round(last_pred.rul_p90, 2),
            "anomaly_score": round(last_pred.anomaly_score, 4),
            "anomaly_ready": last_pred.anomaly_ready,
            "health_index": last_assessment.health_index,
            "status": last_assessment.status,
            "driver": last_assessment.driver,
            "reasons": last_assessment.reasons,
            "maintenance_due": due.isoformat().replace("+00:00", "Z"),
            "cycles_to_maintenance": round(cycles_left, 1),
            "overhauls": overhauls,
            "updated_at": now.isoformat().replace("+00:00", "Z"),
        }
        self.store.upsert_machine_state(state)
        return items, alerts

    @staticmethod
    def _make_alert(item: dict, severity: str, reasons: list[str], driver: str = "none") -> dict:
        kind = "recovery" if severity == "info" else (driver if driver in ("rul", "anomaly") else "rul")
        return {
            "machine_id": item["machine_id"],
            "ts": item["ts"],
            "ts_iso": item["ts_iso"],
            "alert_id": str(uuid.uuid4()),
            "severity": severity,
            "type": kind,
            "status": "open" if severity != "info" else "acknowledged",
            "message": "; ".join(reasons) if reasons else f"Status changed to {severity}",
            "cycle": item["cycle"],
            "rul_p50": item["rul_p50"],
            "anomaly_score": item["anomaly_score"],
            "site": item["site"],
            "line": item["line"],
        }


# ---------------------------------------------------------------------- AWS sinks


def firehose_sink(stream_name: str, region: str | None = None) -> Sink:
    """Archive enriched records as newline-delimited JSON through Kinesis Firehose to S3."""
    import boto3

    client = boto3.client("firehose", region_name=region)

    def _send(items: list[dict]) -> None:
        for i in range(0, len(items), 500):  # Firehose batch limit
            chunk = items[i : i + 500]
            records = [{"Data": (json.dumps(it, separators=(",", ":")) + "\n").encode()} for it in chunk]
            resp = client.put_record_batch(DeliveryStreamName=stream_name, Records=records)
            if resp.get("FailedPutCount"):
                log.warning("firehose: %s records failed", resp["FailedPutCount"])

    return _send


def sns_sink(topic_arn: str, region: str | None = None) -> Sink:
    """Notify operators about critical alerts (email/SMS/Slack via SNS subscriptions)."""
    import boto3

    client = boto3.client("sns", region_name=region)

    def _send(alerts: list[dict]) -> None:
        for a in alerts:
            subject = f"[PdM {a['severity'].upper()}] {a['machine_id']}: {a['type']}"[:100]
            body = (
                f"Machine: {a['machine_id']} ({a['site']}/{a['line']})\n"
                f"Time: {a['ts_iso']}\nCycle: {a['cycle']}\n"
                f"Predicted RUL (p50): {a['rul_p50']} cycles\nAnomaly score: {a['anomaly_score']}\n\n"
                f"{a['message']}\n"
            )
            client.publish(TopicArn=topic_arn, Subject=subject, Message=body)

    return _send
