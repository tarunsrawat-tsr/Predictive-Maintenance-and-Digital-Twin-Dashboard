from __future__ import annotations

import streamlit as st
from common import PUBLIC_DEMO, REFRESH_EVERY, STATUS_EMOJI, load_alerts, store

st.title("🚨 Alerts")
st.caption(
    "Alerts fire on **status transitions** (healthy → warning → critical) and on anomaly threshold crossings, "
    "never per message, so operators are not flooded. Critical alerts are also fanned out via SNS (email/SMS/Slack)."
)

f1, f2, f3 = st.columns(3)
status_filter = f1.selectbox("Status", ["open", "acknowledged", "all"], index=0)
severity_filter = f2.multiselect("Severity", ["critical", "warning", "info"], default=["critical", "warning"])
machine_filter = f3.text_input("Machine contains", "")


@st.fragment(run_every=REFRESH_EVERY)
def live_alerts() -> None:
    df = load_alerts(status=None if status_filter == "all" else status_filter, limit=500)
    if df.empty:
        st.success("No alerts match the current filter.", icon="✅")
        return
    if severity_filter:
        df = df[df["severity"].isin(severity_filter)]
    if machine_filter:
        df = df[df["machine_id"].str.contains(machine_filter, case=False)]

    c = st.columns(4)
    c[0].metric("Shown", len(df))
    c[1].metric("🔴 Critical", int((df["severity"] == "critical").sum()))
    c[2].metric("🟠 Warning", int((df["severity"] == "warning").sum()))
    c[3].metric("Machines affected", df["machine_id"].nunique())

    show = df.copy()
    show["severity"] = show["severity"].map(lambda s: f"{STATUS_EMOJI.get(s, '🔵')} {s}")
    event = st.dataframe(
        show[
            [
                "time",
                "machine_id",
                "severity",
                "type",
                "status",
                "message",
                "cycle",
                "rul_p50",
                "anomaly_score",
                "site",
                "line",
            ]
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="multi-row",
        key="alerts_table",
        column_config={
            "time": st.column_config.DatetimeColumn("time (UTC)", format="YYYY-MM-DD HH:mm:ss"),
            "rul_p50": st.column_config.NumberColumn("RUL p50", format="%.0f"),
            "anomaly_score": st.column_config.NumberColumn("anomaly", format="%.2f"),
        },
        height=min(600, 42 + 35 * len(show)),
    )
    selected = event.selection.rows if event and event.selection else []
    sel_df = df.iloc[selected] if selected else df.iloc[0:0]
    b1, b2, _ = st.columns([1.2, 1.2, 4])
    if PUBLIC_DEMO:
        # Acknowledgement is the only write path in the console; the public demo is read-only.
        b1.button("✅ Acknowledge selected", disabled=True, help="Disabled in the public demo")
    elif b1.button(
        f"✅ Acknowledge selected ({len(sel_df)})", disabled=sel_df.empty or status_filter == "acknowledged"
    ):
        for _, a in sel_df.iterrows():
            store().ack_alert(a["machine_id"], int(a["ts"]), user="console")
        st.toast(f"Acknowledged {len(sel_df)} alert(s)")
        st.rerun()
    if b2.button("Open machine twin", disabled=len(sel_df) != 1):
        st.session_state["selected_machine"] = sel_df.iloc[0]["machine_id"]
        st.switch_page("views/machine.py")


live_alerts()
