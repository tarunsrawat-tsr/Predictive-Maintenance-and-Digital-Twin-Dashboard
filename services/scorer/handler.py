"""AWS Lambda entry point: Kinesis Data Streams -> StreamScorer -> DynamoDB / Firehose / SNS.

Event flow:
    IoT Core topic rule  ->  Kinesis stream (partition key = machine_id)
                         ->  this Lambda (event source mapping, batch window 5s)

The model bundle is loaded once per container (cold start) from S3 (``PDM_MODEL_S3_URI``) or
from the image (``PDM_MODEL_DIR``). Partition by machine_id guarantees per-machine ordering
inside a shard, which the rolling-window features rely on.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

from pydantic import ValidationError

from pdm.config import Settings
from pdm.model import load_bundle
from pdm.schema import TelemetryMessage
from pdm.scoring import StreamScorer, firehose_sink, sns_sink
from pdm.storage import get_store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("pdm.scorer")

_scorer: StreamScorer | None = None


def get_scorer() -> StreamScorer:
    global _scorer
    if _scorer is None:
        t0 = time.time()
        settings = Settings.from_env()
        model = load_bundle(settings.model_dir, settings.model_s3_uri)
        store = get_store(settings)
        archive = (
            firehose_sink(settings.firehose_stream, settings.aws_region) if settings.firehose_stream else None
        )
        notify = sns_sink(settings.sns_topic_arn, settings.aws_region) if settings.sns_topic_arn else None
        _scorer = StreamScorer(model, store, settings, archive_sink=archive, notify_sink=notify)
        log.info(
            "scorer ready in %.2fs (backend=%s, model=%s, window=%d)",
            time.time() - t0,
            settings.backend,
            settings.model_s3_uri or settings.model_dir,
            model.window,
        )
    return _scorer


def parse_kinesis_event(event: dict) -> tuple[list[TelemetryMessage], int]:
    """Decode Kinesis records into messages. Malformed records are logged and dropped
    (poison-pill protection) rather than blocking the shard."""
    messages: list[TelemetryMessage] = []
    dropped = 0
    for rec in event.get("Records", []):
        try:
            payload = base64.b64decode(rec["kinesis"]["data"])
            body = json.loads(payload)
            messages.append(TelemetryMessage.model_validate(body))
        except (KeyError, ValueError, ValidationError) as exc:
            dropped += 1
            log.warning(
                "dropping malformed record seq=%s: %s", rec.get("kinesis", {}).get("sequenceNumber"), exc
            )
    return messages, dropped


def lambda_handler(event: dict, context=None) -> dict:
    messages, dropped = parse_kinesis_event(event)
    if not messages:
        return {"messages": 0, "dropped": dropped}
    t0 = time.time()
    summary = get_scorer().process(messages)
    summary["dropped"] = dropped
    summary["latency_ms"] = round((time.time() - t0) * 1000, 1)
    log.info("batch processed: %s", json.dumps(summary))
    return summary
