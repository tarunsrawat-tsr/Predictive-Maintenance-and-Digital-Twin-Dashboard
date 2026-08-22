"""Business logic: machine health index, status, alert rules, maintenance scheduling and ROI.

Kept free of any I/O so it is trivially unit-testable and shared by the scorer and dashboard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from pdm.config import HealthThresholds, PlantEconomics

STATUS_ORDER = {"healthy": 0, "warning": 1, "critical": 2}


@dataclass
class HealthAssessment:
    health_index: float  # 0..100
    status: str  # healthy | warning | critical
    reasons: list[str]
    driver: str = "none"  # which signal set the status: "rul" | "anomaly" | "none"

    def to_dict(self) -> dict:
        return asdict(self)


def health_index(rul_p50: float, anomaly_score: float, th: HealthThresholds) -> float:
    """Composite 0-100 index: remaining life fraction penalised by anomalous behaviour."""
    life = max(0.0, min(1.0, rul_p50 / th.rul_cap))
    # Anomaly penalty ramps from 0 at score<=1 to 0.6 at score>=critical threshold.
    if anomaly_score <= th.anomaly_warning:
        penalty = 0.0
    else:
        span = max(th.anomaly_critical - th.anomaly_warning, 1e-9)
        penalty = 0.6 * min(1.0, (anomaly_score - th.anomaly_warning) / span)
    return round(100.0 * life * (1.0 - penalty), 1)


def _raw_status(
    rul_p50: float,
    anomaly_score: float,
    rul_warning: float,
    rul_critical: float,
    an_warning: float,
    an_critical: float,
) -> tuple[str, list[str], str]:
    reasons: list[str] = []
    status, driver = "healthy", "none"
    if rul_p50 < rul_critical:
        status, driver = "critical", "rul"
        reasons.append(f"RUL {rul_p50:.0f} cycles < critical threshold {rul_critical:.0f}")
    elif rul_p50 < rul_warning:
        status, driver = "warning", "rul"
        reasons.append(f"RUL {rul_p50:.0f} cycles < warning threshold {rul_warning:.0f}")
    if anomaly_score >= an_critical:
        if status != "critical":
            driver = "anomaly"
        status = "critical"
        reasons.append(f"Anomaly score {anomaly_score:.2f} >= critical {an_critical:.1f}")
    elif anomaly_score >= an_warning:
        if status == "healthy":
            status, driver = "warning", "anomaly"
        reasons.append(f"Anomaly score {anomaly_score:.2f} >= warning {an_warning:.1f}")
    return status, reasons, driver


def assess(
    rul_p50: float, anomaly_score: float, th: HealthThresholds, previous: str | None = None
) -> HealthAssessment:
    """Classify a machine. ``previous`` enables hysteresis: the status is only allowed to improve
    once the metrics clear the thresholds by ``th.hysteresis_*``."""
    status, reasons, driver = _raw_status(
        rul_p50, anomaly_score, th.rul_warning, th.rul_critical, th.anomaly_warning, th.anomaly_critical
    )
    if previous in STATUS_ORDER and STATUS_ORDER[status] < STATUS_ORDER[previous]:
        sticky, sticky_reasons, sticky_driver = _raw_status(
            rul_p50,
            anomaly_score,
            th.rul_warning + th.hysteresis_rul,
            th.rul_critical + th.hysteresis_rul,
            th.anomaly_warning - th.hysteresis_anomaly,
            th.anomaly_critical - th.hysteresis_anomaly,
        )
        # Hold the previous status while still inside the hysteresis band (but never *raise* it).
        if STATUS_ORDER[sticky] > STATUS_ORDER[status]:
            status = sticky if STATUS_ORDER[sticky] <= STATUS_ORDER[previous] else previous
            reasons = [r + " (holding: inside hysteresis band)" for r in sticky_reasons]
            driver = sticky_driver
    return HealthAssessment(health_index(rul_p50, anomaly_score, th), status, reasons, driver)


def worsened(previous: str | None, current: str) -> bool:
    """True when the status degraded (used to de-duplicate alerts)."""
    if previous is None:
        return current != "healthy"
    return STATUS_ORDER[current] > STATUS_ORDER.get(previous, 0)


def recovered(previous: str | None, current: str) -> bool:
    return previous is not None and STATUS_ORDER[current] < STATUS_ORDER.get(previous, 0)


def maintenance_due(
    now: datetime, rul_p10: float, th: HealthThresholds, econ: PlantEconomics
) -> tuple[datetime, float]:
    """Recommended maintenance deadline and the number of cycles until then.

    Uses the conservative p10 RUL minus a safety margin, converted with ``cycle_hours``.
    """
    cycles_left = max(0.0, rul_p10 - th.safety_margin_cycles)
    return now + timedelta(hours=cycles_left * econ.cycle_hours), cycles_left


def schedule_maintenance(machines: list[dict], now: datetime, econ: PlantEconomics) -> list[dict]:
    """Greedy capacity-aware scheduler.

    ``machines`` items need: machine_id, status, rul_p10, maintenance_due (ISO str or datetime).
    Machines are ordered by deadline; each is assigned the earliest crew slot that ends before
    its deadline (or as early as possible if already overdue). Returns plan rows with
    start/end/crew/slack_hours/priority.
    """

    def _dt(v):
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

    jobs = sorted(machines, key=lambda m: _dt(m["maintenance_due"]))
    crews_free_at = [now] * max(1, econ.maintenance_crews)
    duration = timedelta(hours=econ.planned_outage_hours)
    plan = []
    for m in jobs:
        due = _dt(m["maintenance_due"])
        crew = min(range(len(crews_free_at)), key=lambda i: crews_free_at[i])
        start = max(crews_free_at[crew], now)
        # Don't pull work forward unnecessarily: start as late as safely possible but
        # never later than the crew becomes free, and never after the deadline.
        latest_start = due - duration
        if latest_start > start:
            start = latest_start
        end = start + duration
        crews_free_at[crew] = end
        slack_h = (due - end).total_seconds() / 3600.0
        priority = (
            "P1"
            if m.get("status") == "critical" or slack_h < 0
            else ("P2" if m.get("status") == "warning" else "P3")
        )
        plan.append(
            {
                "machine_id": m["machine_id"],
                "status": m.get("status", "healthy"),
                "rul_p10": m.get("rul_p10"),
                "due": due,
                "start": start,
                "end": end,
                "crew": f"Crew {crew + 1}",
                "slack_hours": round(slack_h, 1),
                "priority": priority,
            }
        )
    return sorted(plan, key=lambda r: (r["priority"], r["start"]))


def roi_estimate(
    n_machines: int,
    unplanned_events_per_machine_year: float,
    pdm_capture_rate: float,
    econ: PlantEconomics,
    platform_cost_per_year: float,
) -> dict:
    """Simple avoided-downtime ROI model.

    Each captured failure converts an unplanned outage into a (shorter) planned one.
    """
    events = n_machines * unplanned_events_per_machine_year
    captured = events * pdm_capture_rate
    unplanned_cost = econ.unplanned_outage_hours * econ.downtime_cost_per_hour
    planned_cost = econ.planned_outage_hours * econ.downtime_cost_per_hour
    saving_per_event = unplanned_cost - planned_cost
    gross = captured * saving_per_event
    net = gross - platform_cost_per_year
    return {
        "events_per_year": events,
        "captured_events": captured,
        "saving_per_event": saving_per_event,
        "gross_saving": gross,
        "platform_cost": platform_cost_per_year,
        "net_benefit": net,
        "roi_pct": (net / platform_cost_per_year * 100.0) if platform_cost_per_year else float("inf"),
        "payback_months": (platform_cost_per_year / gross * 12.0) if gross > 0 else float("inf"),
        "downtime_hours_avoided": captured * (econ.unplanned_outage_hours - econ.planned_outage_hours),
    }
