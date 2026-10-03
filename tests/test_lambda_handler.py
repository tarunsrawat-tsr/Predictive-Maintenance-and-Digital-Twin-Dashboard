"""Exercise the Lambda entry point with a synthetic Kinesis event (local store backend)."""

from __future__ import annotations

import base64

import pytest

from pdm.simulator import FleetSimulator


def _kinesis_event(messages, extra_bad: bool = True) -> dict:
    records = [
        {
            "kinesis": {
                "sequenceNumber": str(i),
                "partitionKey": m.machine_id,
                "data": base64.b64encode(m.model_dump_json().encode()).decode(),
            },
            "eventSource": "aws:kinesis",
        }
        for i, m in enumerate(messages)
    ]
    if extra_bad:
        records.append({"kinesis": {"sequenceNumber": "bad", "data": base64.b64encode(b"not json").decode()}})
        records.append(
            {"kinesis": {"sequenceNumber": "bad2", "data": base64.b64encode(b'{"machine_id": "X"}').decode()}}
        )
    return {"Records": records}


@pytest.fixture
def handler(monkeypatch, model_dir, tmp_path):
    monkeypatch.setenv("PDM_BACKEND", "local")
    monkeypatch.setenv("PDM_LOCAL_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("PDM_MODEL_DIR", str(model_dir))
    import handler as h

    h._scorer = None  # reset the module-level singleton between tests
    return h


def test_parse_drops_malformed_records(handler, synth_data_dir):
    sim = FleetSimulator(n_machines=2, data_dir=str(synth_data_dir), virtual_tick_seconds=60)
    msgs = sim.step()
    parsed, dropped = handler.parse_kinesis_event(_kinesis_event(msgs))
    assert len(parsed) == 2 and dropped == 2


def test_lambda_handler_end_to_end(handler, synth_data_dir):
    sim = FleetSimulator(n_machines=3, data_dir=str(synth_data_dir), virtual_tick_seconds=60)
    total = 0
    for _ in range(5):
        out = handler.lambda_handler(_kinesis_event(sim.step(), extra_bad=False), None)
        total += out["messages"]
        assert out["machines"] == 3 and "latency_ms" in out
    assert total == 15
    states = handler.get_scorer().store.list_machine_states()
    assert {s["machine_id"] for s in states} == {"GT-001", "GT-002", "GT-003"}
    # Empty / all-bad batches are a no-op, not an error.
    assert handler.lambda_handler({"Records": []}, None) == {"messages": 0, "dropped": 0}
    bad = handler.lambda_handler(_kinesis_event([], extra_bad=True), None)
    assert bad == {"messages": 0, "dropped": 2}


def test_firehose_and_sns_sinks_with_moto(monkeypatch):
    import boto3
    from moto import mock_aws

    from pdm.scoring import firehose_sink, sns_sink

    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(Bucket="lake", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        fh = boto3.client("firehose", region_name="ap-northeast-1")
        fh.create_delivery_stream(
            DeliveryStreamName="archive",
            DeliveryStreamType="DirectPut",
            ExtendedS3DestinationConfiguration={
                "RoleARN": "arn:aws:iam::123456789012:role/firehose",
                "BucketARN": "arn:aws:s3:::lake",
            },
        )
        sink = firehose_sink("archive", "ap-northeast-1")
        sink([{"machine_id": "GT-001", "ts": 1, "rul_p50": 42.0}] * 3)
        keys = s3.list_objects_v2(Bucket="lake").get("Contents", [])
        assert keys, "firehose should have delivered to S3"
        body = s3.get_object(Bucket="lake", Key=keys[0]["Key"])["Body"].read().decode()
        assert body.count("\n") == 3  # newline-delimited JSON for Athena

        sns = boto3.client("sns", region_name="ap-northeast-1")
        topic = sns.create_topic(Name="alerts")["TopicArn"]
        notify = sns_sink(topic, "ap-northeast-1")
        notify(
            [
                {
                    "machine_id": "GT-001",
                    "severity": "critical",
                    "type": "rul",
                    "site": "nagoya",
                    "line": "L1",
                    "ts_iso": "2026-10-03T00:00:00Z",
                    "cycle": 190,
                    "rul_p50": 12.3,
                    "anomaly_score": 1.4,
                    "message": "RUL 12 cycles < critical threshold 20",
                }
            ]  # fmt: skip
        )
