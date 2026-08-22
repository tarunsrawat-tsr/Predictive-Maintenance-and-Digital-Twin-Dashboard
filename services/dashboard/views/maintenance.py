from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import streamlit as st
from charts import maintenance_gantt, roi_waterfall
from common import load_states, settings

from pdm.config import PlantEconomics
from pdm.health import maintenance_due, roi_estimate, schedule_maintenance

base = settings().economics
th = settings().thresholds

st.title("🗓️ Maintenance schedule & business case")
st.caption(
    "The schedule converts each machine's conservative RUL (p10) into a deadline, then assigns crews so that work is "
    "done just-in-time without exceeding capacity. The ROI model answers leadership's question: is this worth it?"
)

with st.sidebar:
    st.markdown("### Plant assumptions")
    cycle_hours = st.number_input("Hours per machine cycle", 0.5, 48.0, float(base.cycle_hours), 0.5)
    planned_h = st.number_input(
        "Planned maintenance duration (h)", 1.0, 72.0, float(base.planned_outage_hours), 1.0
    )
    unplanned_h = st.number_input(
        "Unplanned outage duration (h)", 1.0, 240.0, float(base.unplanned_outage_hours), 1.0
    )
    crews = st.number_input("Maintenance crews available", 1, 10, int(base.maintenance_crews))
    cost_h = st.number_input(
        "Downtime cost (USD / hour)",
        100.0,
        1_000_000.0,
        float(base.downtime_cost_per_hour),
        500.0,
        format="%.0f",
    )

econ = PlantEconomics(
    cycle_hours=cycle_hours,
    downtime_cost_per_hour=cost_h,
    unplanned_outage_hours=unplanned_h,
    planned_outage_hours=planned_h,
    maintenance_crews=int(crews),
)

states = load_states()
now = datetime.now(UTC)

st.subheader("Recommended plan")
if states.empty:
    st.info("No machine state yet.")
else:
    # Recompute deadlines with the (possibly edited) cycle-hours assumption.
    machines = []
    for _, r in states.iterrows():
        due, _ = maintenance_due(now, float(r["rul_p10"]), th, econ)
        machines.append(
            {
                "machine_id": r["machine_id"],
                "status": r["status"],
                "rul_p10": float(r["rul_p10"]),
                "maintenance_due": due,
            }
        )
    horizon_days = st.slider("Planning horizon (days)", 1, 90, 30)
    plan = pd.DataFrame(schedule_maintenance(machines, now, econ))
    plan = plan[plan["due"] <= now + pd.Timedelta(days=horizon_days)]
    if plan.empty:
        st.success(f"Nothing due within {horizon_days} days.", icon="✅")
    else:
        c = st.columns(4)
        c[0].metric("Jobs in horizon", len(plan))
        c[1].metric("P1 (urgent)", int((plan["priority"] == "P1").sum()))
        c[2].metric("Crew hours", f"{len(plan) * planned_h:,.0f} h")
        c[3].metric(
            "Infeasible (negative slack)",
            int((plan["slack_hours"] < 0).sum()),
            help="Jobs that cannot be completed before their deadline with the current crew capacity.",
        )
        st.plotly_chart(maintenance_gantt(plan), width="stretch", config={"displayModeBar": False})
        show = plan.copy()
        for col in ("due", "start", "end"):
            show[col] = pd.to_datetime(show[col]).dt.strftime("%Y-%m-%d %H:%M")
        st.dataframe(
            show[["priority", "machine_id", "status", "rul_p10", "due", "start", "end", "crew", "slack_hours"]],
            hide_index=True, width="stretch",
            column_config={"rul_p10": st.column_config.NumberColumn("RUL p10", format="%.0f"), "slack_hours": st.column_config.NumberColumn("slack (h)", format="%.1f")},
        )  # fmt: skip
        csv = show.to_csv(index=False).encode()
        st.download_button("⬇️ Export plan (CSV)", csv, file_name="maintenance_plan.csv", mime="text/csv")

st.divider()
st.subheader("Business case · is the investment worth it?")
left, right = st.columns([1, 1.4])
with left:
    n_machines = st.number_input(
        "Machines in scope", 1, 10_000, max(int(len(states)), 1) if not states.empty else 20
    )
    events = st.number_input("Unplanned failures per machine per year (baseline)", 0.0, 20.0, 1.2, 0.1)
    capture = st.slider(
        "Share of failures caught early by PdM",
        0.0,
        1.0,
        0.7,
        0.05,
        help="Recall of the RUL/anomaly models at the chosen thresholds; 60–80% is typical for a first deployment.",
    )
    platform = st.number_input(
        "Platform run cost per year (USD)",
        0.0,
        10_000_000.0,
        60_000.0,
        5_000.0,
        format="%.0f",
        help="Cloud + support + sensor retrofits amortised. The demo stack itself costs ≈ $60–80/month.",
    )
    roi = roi_estimate(int(n_machines), events, capture, econ, platform)
    m = st.columns(2)
    m[0].metric("Net annual benefit", f"${roi['net_benefit']:,.0f}")
    m[1].metric("ROI", f"{roi['roi_pct']:,.0f}%")
    m[0].metric(
        "Payback", f"{roi['payback_months']:.1f} months" if roi["payback_months"] != float("inf") else "–"
    )
    m[1].metric("Downtime hours avoided / yr", f"{roi['downtime_hours_avoided']:,.0f} h")
    st.caption(
        f"Each caught failure converts a {unplanned_h:.0f} h unplanned outage into a {planned_h:.0f} h planned one, "
        f"saving ${roi['saving_per_event']:,.0f}. {roi['captured_events']:.1f} of {roi['events_per_year']:.1f} yearly failures are caught."
    )
with right:
    st.plotly_chart(roi_waterfall(roi), width="stretch", config={"displayModeBar": False})
