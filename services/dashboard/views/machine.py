from __future__ import annotations

import pandas as pd
import streamlit as st
from charts import anomaly_timeline, engine_twin, health_gauge, rul_trajectory, sensor_trends
from common import (
    REFRESH_SECONDS,
    badge,
    fmt_dt,
    humanize_delta,
    load_alerts,
    load_states,
    load_telemetry,
    model_bundle,
    sensor_label,
    settings,
)

from pdm.schema import INFORMATIVE_SENSORS, SENSOR_COLUMNS

th = settings().thresholds
econ = settings().economics

st.title("⚙️ Machine twin")

states = load_states()
if states.empty:
    st.warning("No machines reporting yet.", icon="⏳")
    st.stop()

ids = states["machine_id"].tolist()
default = st.session_state.get("selected_machine", ids[0])
c1, c2, c3 = st.columns([2, 3, 2])
machine_id = c1.selectbox(
    "Machine", ids, index=ids.index(default) if default in ids else 0, key="selected_machine"
)
sensors = c2.multiselect(
    "Sensors to plot",
    options=SENSOR_COLUMNS,
    default=["s4", "s11", "s12", "s15"],
    format_func=sensor_label,
    max_selections=8,
)
history = c3.slider("History (points)", 50, 1000, 300, 50)
x_axis = st.radio("X axis", ["cycle", "time"], horizontal=True, label_visibility="collapsed")


def _zscores(tele: pd.DataFrame, bundle) -> dict[str, float]:
    """Deviation of the latest reading from the healthy baseline, per informative sensor.

    Uses the anomaly detector's baseline mean/scale (fleet-wide healthy state) when the model
    bundle is available; otherwise the machine's own first readings in the loaded window.
    """
    if tele.empty:
        return {}
    out: dict[str, float] = {}
    if bundle is not None:
        k = len(INFORMATIVE_SENSORS)
        mean, scale = bundle.anomaly.mean[:k], bundle.anomaly.scale[:k]
        last = tele.iloc[-1]
        for i, s in enumerate(INFORMATIVE_SENSORS):
            out[s] = float((last[s] - mean[i]) / (scale[i] or 1.0))
        return out
    base = tele.head(max(5, len(tele) // 5))
    last = tele.iloc[-1]
    for s in INFORMATIVE_SENSORS:
        sd = base[s].std() or 1.0
        out[s] = float((last[s] - base[s].mean()) / sd)
    return out


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def live_machine() -> None:
    states = load_states()
    row = states[states["machine_id"] == machine_id]
    if row.empty:
        st.info("Machine not found.")
        return
    row = row.iloc[0]
    tele = load_telemetry(machine_id, limit=history)
    bundle = model_bundle()

    st.markdown(
        f"## {machine_id} {badge(row['status'])} &nbsp; <span class='pdm-muted'>{row['site']} / {row['line']} · "
        f"cycle {int(row['cycle'])} · last seen {humanize_delta(row['last_seen'])} ago · overhauls {int(row.get('overhauls', 0) or 0)}</span>",
        unsafe_allow_html=True,
    )
    m = st.columns([1.3, 1, 1, 1, 1, 1.3])
    with m[0]:
        st.plotly_chart(
            health_gauge(float(row["health_index"]), row["status"]),
            width="stretch",
            config={"displayModeBar": False},
        )
    m[1].metric("RUL p50", f"{row['rul_p50']:.0f} cycles", help="Median predicted remaining useful life")
    m[2].metric(
        "RUL p10 – p90",
        f"{row['rul_p10']:.0f} – {row['rul_p90']:.0f}",
        help="80% conformal prediction interval",
    )
    m[3].metric(
        "Anomaly score",
        f"{row['anomaly_score']:.2f}",
        delta=f"warn ≥ {th.anomaly_warning:.1f}",
        delta_color="off",
    )
    m[4].metric(
        "Maintain by", humanize_delta(row["maintenance_due_dt"]), help=fmt_dt(row["maintenance_due_dt"])
    )
    with m[5]:
        st.markdown("**Why this status**")
        reasons = row.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if reasons:
            for r in reasons:
                st.markdown(f"- {r}")
        else:
            st.markdown("- Within all thresholds")
        st.caption(
            f"Deadline = p10 RUL − {th.safety_margin_cycles:.0f} cycle margin, {econ.cycle_hours:.0f} h/cycle"
        )

    if tele.empty:
        st.info("No telemetry for this machine yet.")
        return

    z = _zscores(tele, bundle)
    st.plotly_chart(
        engine_twin(tele.iloc[-1], z, INFORMATIVE_SENSORS), width="stretch", config={"displayModeBar": False}
    )

    a, b = st.columns([3, 2])
    a.plotly_chart(
        rul_trajectory(tele, th.rul_warning, th.rul_critical, th.rul_cap, x=x_axis),
        width="stretch",
        config={"displayModeBar": False},
    )
    b.plotly_chart(
        anomaly_timeline(tele, th.anomaly_warning, th.anomaly_critical, x=x_axis),
        width="stretch",
        config={"displayModeBar": False},
    )

    if sensors:
        st.plotly_chart(
            sensor_trends(tele, sensors, x=x_axis), width="stretch", config={"displayModeBar": False}
        )

    with st.expander("Model inputs · latest feature window", expanded=False):
        window = bundle.window if bundle else 20
        if bundle is not None:
            st.caption(
                f"The scorer rebuilds a {window}-cycle window per machine and computes last/mean/std/slope for "
                f"{len(INFORMATIVE_SENSORS)} informative sensors. Anomaly = Mahalanobis distance of the (last, mean) vector "
                f"to the healthy fleet baseline (threshold {bundle.anomaly.threshold:.2f} ⇒ score 1.0)."
            )
        cols = [
            "cycle",
            "time",
            "rul_p50",
            "anomaly_score",
            "health_index",
            *[s for s in INFORMATIVE_SENSORS if s in tele],
        ]
        st.dataframe(tele.tail(window)[cols], hide_index=True, width="stretch")

    alerts = load_alerts(limit=500)
    mine = alerts[alerts["machine_id"] == machine_id].head(10) if not alerts.empty else alerts
    st.subheader("Alert history")
    if mine.empty:
        st.caption("No alerts for this machine.")
    else:
        st.dataframe(
            mine[["time", "severity", "type", "status", "message", "cycle", "rul_p50", "anomaly_score"]],
            hide_index=True, width="stretch",
            column_config={"time": st.column_config.DatetimeColumn("time (UTC)", format="YYYY-MM-DD HH:mm:ss")},
        )  # fmt: skip


live_machine()
