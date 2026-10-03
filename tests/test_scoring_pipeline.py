"""End-to-end test of the streaming scorer against the SQLite store and (via moto) DynamoDB."""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from pdm.config import Settings
from pdm.scoring import StreamScorer
from pdm.simulator import FleetSimulator
from pdm.storage.dynamodb import DynamoStore, from_ddb, to_ddb
from pdm.storage.local import SQLiteStore


def _run_fleet(store, bundle, data_dir, ticks=60, n_machines=3):
    settings = Settings(backend="local")
    archived: list[dict] = []
    notified: list[dict] = []
    scorer = StreamScorer(bundle, store, settings, archive_sink=archived.extend, notify_sink=notified.extend)
    sim = FleetSimulator(
        n_machines=n_machines, data_dir=str(data_dir), seed=3, start_offset="random", virtual_tick_seconds=60
    )
    summaries = [scorer.process(msgs) for msgs in sim.stream(max_ticks=ticks)]
    return sim, summaries, archived, notified


def test_pipeline_sqlite_end_to_end(bundle, synth_data_dir):
    store = SQLiteStore(":memory:")
    sim, summaries, archived, notified = _run_fleet(store, bundle, synth_data_dir, ticks=80)
    assert sum(s["messages"] for s in summaries) == 80 * 3 == len(archived)

    states = store.list_machine_states()
    assert [s["machine_id"] for s in states] == ["GT-001", "GT-002", "GT-003"]
    for s in states:
        assert s["status"] in {"healthy", "warning", "critical"}
        assert 0 <= s["health_index"] <= 100
        assert s["rul_p10"] <= s["rul_p50"] <= s["rul_p90"]
        assert s["maintenance_due"].endswith("Z")

    tele = store.get_telemetry("GT-001", limit=50)
    assert len(tele) == 50
    assert [t["ts"] for t in tele] == sorted(t["ts"] for t in tele)  # chronological
    assert "rul_p50" in tele[-1] and "sensors" in tele[-1]

    # Machines in the synthetic fleet drift to failure -> at least one alert must have fired.
    alerts = store.list_alerts()
    assert alerts, "expected at least one status-transition alert"
    assert alerts[0]["ts"] >= alerts[-1]["ts"]  # newest first
    # Alerts are de-duplicated: exactly one alert per status *transition* that worsens the
    # machine (or brings it back to healthy), never one per message.
    from pdm.health import recovered, worsened

    for mid in ("GT-001", "GT-002", "GT-003"):
        tele_all = store.get_telemetry(mid, limit=1000)
        prev, prev_cycle, expected = None, None, 0
        for t in tele_all:
            if t["cycle"] == 1 or (prev_cycle is not None and t["cycle"] < prev_cycle):
                prev = None  # overhaul: state machine restarts
            st = t["status"]
            if worsened(prev, st) or (recovered(prev, st) and st == "healthy"):
                expected += 1
            prev, prev_cycle = st, t["cycle"]
        assert sum(a["machine_id"] == mid for a in alerts) == expected
    crit = [a for a in alerts if a["severity"] == "critical"]
    assert len(notified) == len(crit)

    # Acknowledge workflow
    open_alerts = store.list_alerts(status="open")
    if open_alerts:
        a = open_alerts[0]
        store.ack_alert(a["machine_id"], a["ts"], user="tester")
        assert all(
            x["ts"] != a["ts"] for x in store.list_alerts(status="open") if x["machine_id"] == a["machine_id"]
        )


def test_overhaul_resets_feature_window(bundle, synth_data_dir):
    """When a machine is replaced (cycle restarts at 1) old history must not leak into features."""
    store = SQLiteStore(":memory:")
    settings = Settings(backend="local")
    scorer = StreamScorer(bundle, store, settings)
    sim = FleetSimulator(
        n_machines=1, data_dir=str(synth_data_dir), seed=1, start_offset="zero", virtual_tick_seconds=60
    )
    # Fast-forward the single machine to its last few rows
    m = sim.machines[0]
    m.pos = len(m.rows) - 2
    seen_cycles = []
    for msgs in sim.stream(max_ticks=6):
        scorer.process(msgs)
        seen_cycles.append(msgs[0].cycle)
    assert 1 in seen_cycles and seen_cycles[-1] < 10  # overhaul happened
    state = store.get_machine_state("GT-001")
    assert state["overhauls"] == 1
    # First cycle after overhaul: window is 1 row -> std/slope features are 0 -> prediction equals
    # the prediction for a brand-new machine with the same reading.
    tele = store.get_telemetry("GT-001", limit=10)
    first_new = next(t for t in tele if t["cycle"] == 1)
    import numpy as np

    from pdm.storage.base import raw_vector_from_item

    p = bundle.predict_window(np.array([raw_vector_from_item(first_new)]), 1)
    assert abs(p.rul_p50 - first_new["rul_p50"]) < 0.01  # stored values are rounded to 2 dp


def test_ddb_codec_roundtrip():
    item = {"a": 1.5, "b": {"c": [1, 2.25]}, "d": "x", "e": 3}
    out = from_ddb(to_ddb(item))
    assert out == item and isinstance(out["e"], int) and isinstance(out["a"], float)


@pytest.fixture
def ddb_tables():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
        ddb.create_table(
            TableName="t-telemetry",
            KeySchema=[
                {"AttributeName": "machine_id", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "machine_id", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="t-state",
            KeySchema=[{"AttributeName": "machine_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "machine_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="t-alerts",
            KeySchema=[
                {"AttributeName": "machine_id", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "machine_id", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "N"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-ts-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "ts", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_pipeline_dynamodb_with_moto(ddb_tables, bundle, synth_data_dir):
    store = DynamoStore("t-telemetry", "t-state", "t-alerts", region="ap-northeast-1")
    _, summaries, _, _ = _run_fleet(store, bundle, synth_data_dir, ticks=40, n_machines=2)
    assert sum(s["messages"] for s in summaries) == 80
    states = store.list_machine_states()
    assert len(states) == 2 and isinstance(states[0]["rul_p50"], float)
    tele = store.get_telemetry("GT-002", limit=15)
    assert len(tele) == 15 and tele[0]["ts"] <= tele[-1]["ts"]
    assert "ttl" in tele[0]
    alerts = store.list_alerts()
    if alerts:
        a = alerts[0]
        store.ack_alert(a["machine_id"], a["ts"])
        acked = store.list_alerts(status="acknowledged")
        assert any(x["ts"] == a["ts"] and x["machine_id"] == a["machine_id"] for x in acked)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("PDM_BACKEND", "dynamodb")
    monkeypatch.setenv("PDM_RUL_CRITICAL", "15")
    s = Settings.from_env()
    assert s.backend == "dynamodb" and s.thresholds.rul_critical == 15
    assert os.environ["PDM_BACKEND"] == "dynamodb"
