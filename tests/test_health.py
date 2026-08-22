from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pdm.config import HealthThresholds, PlantEconomics
from pdm.health import (
    assess,
    health_index,
    maintenance_due,
    recovered,
    roi_estimate,
    schedule_maintenance,
    worsened,
)

TH = HealthThresholds()
ECON = PlantEconomics(cycle_hours=8, maintenance_crews=2, planned_outage_hours=6)


def test_health_index_bounds():
    assert health_index(125, 0.5, TH) == 100.0
    assert health_index(0, 0.0, TH) == 0.0
    assert health_index(62.5, 0.0, TH) == 50.0
    # anomaly penalty kicks in above the warning threshold
    assert health_index(125, 2.0, TH) == 40.0
    assert health_index(125, 1.5, TH) == 70.0


def test_assess_thresholds():
    assert assess(100, 0.5, TH).status == "healthy"
    assert assess(40, 0.5, TH).status == "warning"
    assert assess(10, 0.5, TH).status == "critical"
    assert assess(100, 1.2, TH).status == "warning"
    assert assess(100, 2.5, TH).status == "critical"
    a = assess(10, 2.5, TH)
    assert a.status == "critical" and len(a.reasons) == 2


def test_transitions():
    assert worsened(None, "warning")
    assert not worsened(None, "healthy")
    assert worsened("healthy", "warning")
    assert worsened("warning", "critical")
    assert not worsened("critical", "warning")
    assert recovered("critical", "healthy")
    assert not recovered(None, "healthy")


def test_maintenance_due_uses_safety_margin():
    now = datetime(2026, 10, 3, tzinfo=UTC)
    due, cycles = maintenance_due(now, rul_p10=30, th=TH, econ=ECON)
    assert cycles == 20
    assert due == now + timedelta(hours=160)
    due2, cycles2 = maintenance_due(now, rul_p10=5, th=TH, econ=ECON)
    assert cycles2 == 0 and due2 == now


def test_schedule_respects_crew_capacity_and_priority():
    now = datetime(2026, 10, 3, tzinfo=UTC)
    machines = [
        {"machine_id": "A", "status": "critical", "rul_p10": 2, "maintenance_due": now.isoformat()},
        {
            "machine_id": "B",
            "status": "critical",
            "rul_p10": 3,
            "maintenance_due": (now + timedelta(hours=2)).isoformat(),
        },
        {
            "machine_id": "C",
            "status": "warning",
            "rul_p10": 30,
            "maintenance_due": (now + timedelta(hours=3)).isoformat(),
        },
        {
            "machine_id": "D",
            "status": "healthy",
            "rul_p10": 90,
            "maintenance_due": (now + timedelta(days=20)).isoformat(),
        },
    ]
    plan = schedule_maintenance(machines, now, ECON)
    by_id = {p["machine_id"]: p for p in plan}
    # Two crews: A and B start immediately; C must wait for a crew -> negative slack -> P1
    assert by_id["A"]["start"] == now and by_id["B"]["start"] == now
    assert by_id["C"]["start"] >= now + timedelta(hours=6)
    assert by_id["C"]["priority"] == "P1" and by_id["C"]["slack_hours"] < 0
    # Healthy machine with plenty of time is scheduled just-in-time, not immediately.
    assert by_id["D"]["priority"] == "P3" and by_id["D"]["start"] > now + timedelta(days=10)
    assert plan[0]["priority"] == "P1" and plan[-1]["priority"] == "P3"


def test_roi_estimate():
    r = roi_estimate(
        n_machines=20, unplanned_events_per_machine_year=1.0, pdm_capture_rate=0.7,
        econ=PlantEconomics(downtime_cost_per_hour=10_000, unplanned_outage_hours=24, planned_outage_hours=6),
        platform_cost_per_year=100_000,
    )  # fmt: skip
    assert r["captured_events"] == 14
    assert r["saving_per_event"] == 180_000
    assert r["gross_saving"] == 14 * 180_000
    assert r["net_benefit"] == 14 * 180_000 - 100_000
    assert 0 < r["payback_months"] < 1


def test_hysteresis_prevents_flapping():
    # Fell into warning at RUL 49 ...
    assert assess(49, 0.5, TH, previous="healthy").status == "warning"
    # ... RUL creeps back to 52: still inside the 8-cycle band -> stays warning
    held = assess(52, 0.5, TH, previous="warning")
    assert held.status == "warning" and "holding" in held.reasons[0]
    # ... clears the band -> healthy again
    assert assess(59, 0.5, TH, previous="warning").status == "healthy"
    # Hysteresis never upgrades: a healthy machine at RUL 52 stays healthy.
    assert assess(52, 0.5, TH, previous="healthy").status == "healthy"
    # Critical -> metrics improve to warning band only -> drops to warning, not healthy.
    assert assess(45, 0.5, TH, previous="critical").status == "warning"
    assert assess(22, 0.5, TH, previous="critical").status == "critical"
    # Anomaly hysteresis
    assert assess(100, 0.9, TH, previous="warning").status == "warning"
    assert assess(100, 0.7, TH, previous="warning").status == "healthy"
