from __future__ import annotations

import pandas as pd
import streamlit as st
from charts import fleet_rul_bars, plant_floor
from common import REFRESH_SECONDS, STATUS_EMOJI, fmt_dt, load_alerts, load_states, machine_card, settings

th = settings().thresholds
econ = settings().economics

st.title("🏭 Fleet overview")
st.caption(
    "Live digital-twin view of every monitored asset: predicted remaining useful life (RUL), anomaly score, health index and maintenance deadline."
)


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def live_fleet() -> None:
    states = load_states()
    alerts_open = load_alerts(status="open", limit=500)

    if states.empty:
        st.warning(
            "No machine state yet. Start the simulator (or wait for the first Kinesis batch to be scored).",
            icon="⏳",
        )
        return

    n = len(states)
    counts = states["status"].value_counts()
    crit, warn, ok = (
        int(counts.get("critical", 0)),
        int(counts.get("warning", 0)),
        int(counts.get("healthy", 0)),
    )
    at_risk = states[states["status"] != "healthy"]
    # Money at risk if the non-healthy machines failed unplanned instead of being maintained.
    exposure = (
        len(at_risk) * (econ.unplanned_outage_hours - econ.planned_outage_hours) * econ.downtime_cost_per_hour
    )
    next_due = states["maintenance_due_dt"].min()
    stale = states[states["last_seen"] < pd.Timestamp.now("UTC") - pd.Timedelta(minutes=10)]

    k = st.columns(6)
    k[0].metric(
        "Machines online", n, delta=f"-{len(stale)} stale" if len(stale) else None, delta_color="inverse"
    )
    k[1].metric("Fleet health", f"{states['health_index'].mean():.0f}%")
    k[2].metric("🔴 Critical / 🟠 Warning", f"{crit} / {warn}", delta=f"{ok} healthy", delta_color="off")
    k[3].metric("Open alerts", len(alerts_open))
    k[4].metric("Next maintenance", fmt_dt(next_due)[:16] if pd.notna(next_due) else "–")
    k[5].metric(
        "Downtime cost avoided*",
        f"${exposure:,.0f}",
        help="*(unplanned − planned outage hours) × downtime cost/h for every machine currently flagged. Assumptions are editable on the Maintenance & ROI page.",
    )

    left, right = st.columns([3, 2])
    with left:
        st.plotly_chart(plant_floor(states), width="stretch", config={"displayModeBar": False})
    with right:
        st.plotly_chart(
            fleet_rul_bars(states, th.rul_warning, th.rul_critical),
            width="stretch",
            config={"displayModeBar": False},
        )

    st.subheader("Asset cards")
    sort_key = st.session_state.get("fleet_sort", "risk")
    order = states.copy()
    order["_rank"] = order["status"].map({"critical": 0, "warning": 1, "healthy": 2, "unknown": 3})
    order = order.sort_values(["_rank", "rul_p50"]) if sort_key == "risk" else order.sort_values("machine_id")
    cols = st.columns(4)
    for i, (_, row) in enumerate(order.iterrows()):
        with cols[i % 4]:
            st.markdown(machine_card(row), unsafe_allow_html=True)

    if not alerts_open.empty:
        st.subheader("Latest open alerts")
        show = alerts_open.head(8).copy()
        show["sev"] = show["severity"].map(lambda s: f"{STATUS_EMOJI.get(s, '🔵')} {s}")
        st.dataframe(
            show[["time", "machine_id", "sev", "type", "message", "rul_p50", "anomaly_score"]],
            hide_index=True, width="stretch",
            column_config={"time": st.column_config.DatetimeColumn("time (UTC)", format="YYYY-MM-DD HH:mm:ss")},
        )  # fmt: skip


st.radio(
    "Sort cards by",
    options=["risk", "machine_id"],
    key="fleet_sort",
    horizontal=True,
    label_visibility="collapsed",
)
live_fleet()
