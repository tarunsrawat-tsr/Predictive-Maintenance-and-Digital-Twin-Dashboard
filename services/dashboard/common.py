"""Shared helpers for the Streamlit dashboard: data access, caching, styling."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pdm.config import Settings  # noqa: E402
from pdm.schema import SENSOR_COLUMNS, SENSOR_META  # noqa: E402
from pdm.storage import get_store  # noqa: E402

STATUS_COLORS = {"healthy": "#22c55e", "warning": "#f59e0b", "critical": "#ef4444", "unknown": "#64748b"}
STATUS_EMOJI = {"healthy": "🟢", "warning": "🟠", "critical": "🔴", "unknown": "⚪"}
SEVERITY_COLORS = {"critical": "#ef4444", "warning": "#f59e0b", "info": "#38bdf8"}
REFRESH_SECONDS = int(os.environ.get("PDM_DASHBOARD_REFRESH_SECONDS", "5"))

CSS = """
<style>
:root { --card-bg: #0f172a; --card-border: #1e293b; }
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
.pdm-card { background: var(--card-bg); border: 1px solid var(--card-border); border-left: 6px solid #64748b;
            border-radius: 10px; padding: 0.8rem 0.9rem; margin-bottom: 0.6rem; min-height: 150px; }
.pdm-card.healthy { border-left-color: #22c55e; } .pdm-card.warning { border-left-color: #f59e0b; }
.pdm-card.critical { border-left-color: #ef4444; animation: pdm-pulse 1.6s ease-in-out infinite; }
@keyframes pdm-pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.0);} 50% { box-shadow: 0 0 0 4px rgba(239,68,68,0.25);} }
.pdm-card h4 { margin: 0 0 0.25rem 0; font-size: 1.05rem; }
.pdm-card .sub { color: #94a3b8; font-size: 0.78rem; margin-bottom: 0.35rem; }
.pdm-card .row { display: flex; justify-content: space-between; font-size: 0.86rem; margin: 0.12rem 0; }
.pdm-card .row b { font-variant-numeric: tabular-nums; }
.pdm-bar { height: 7px; background: #1e293b; border-radius: 4px; overflow: hidden; margin-top: 0.4rem; }
.pdm-bar > div { height: 100%; }
.pdm-badge { display:inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; color: #0b1220; }
.pdm-muted { color:#94a3b8; font-size:0.8rem; }
[data-testid="stMetricValue"] { font-size: 1.6rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def settings() -> Settings:
    return Settings.from_env()


@st.cache_resource(show_spinner=False)
def store():
    return get_store(settings())


@st.cache_resource(show_spinner=False)
def model_bundle():
    """Model bundle is optional for the dashboard (used for the model card and sensor baselines)."""
    try:
        from pdm.model import load_bundle

        s = settings()
        return load_bundle(s.model_dir, s.model_s3_uri)
    except Exception:  # pragma: no cover - missing model is fine
        return None


# ---------------------------------------------------------------------- data loaders
def load_states() -> pd.DataFrame:
    rows = store().list_machine_states()
    if not rows:
        return pd.DataFrame(
            columns=["machine_id", "site", "line", "status", "health_index", "rul_p10", "rul_p50", "rul_p90",
                     "anomaly_score", "cycle", "maintenance_due", "last_ts", "reasons"]
        )  # fmt: skip
    df = pd.DataFrame(rows)
    df["maintenance_due_dt"] = pd.to_datetime(df["maintenance_due"], utc=True, errors="coerce")
    df["last_seen"] = pd.to_datetime(df["last_ts"], unit="ms", utc=True, errors="coerce")
    df["status"] = df["status"].fillna("unknown")
    return df.sort_values("machine_id").reset_index(drop=True)


def load_telemetry(machine_id: str, limit: int = 400) -> pd.DataFrame:
    rows = store().get_telemetry(machine_id, limit=limit)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    sensors = pd.DataFrame(list(df["sensors"]))
    df = pd.concat([df.drop(columns=["sensors", "settings"], errors="ignore"), sensors], axis=1)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def load_alerts(status: str | None = None, limit: int = 300) -> pd.DataFrame:
    rows = store().list_alerts(status=status, limit=limit)
    if not rows:
        return pd.DataFrame(
            columns=[
                "machine_id",
                "ts",
                "severity",
                "type",
                "status",
                "message",
                "cycle",
                "rul_p50",
                "anomaly_score",
            ]
        )
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.sort_values("ts", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------- formatting helpers
def sensor_label(col: str) -> str:
    m = SENSOR_META.get(col)
    return f"{col} · {m['name']} ({m['unit']})" if m else col


def sensor_options() -> list[str]:
    return [c for c in SENSOR_COLUMNS]


def fmt_dt(dt) -> str:
    if dt is None or pd.isna(dt):
        return "–"
    if isinstance(dt, str):
        dt = pd.to_datetime(dt, utc=True)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def humanize_delta(dt) -> str:
    if dt is None or pd.isna(dt):
        return "–"
    now = datetime.now(UTC)
    delta = (dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt) - now
    secs = delta.total_seconds()
    sign = "" if secs >= 0 else "-"
    secs = abs(secs)
    if secs < 3600:
        return f"{sign}{int(secs // 60)} min"
    if secs < 86400:
        return f"{sign}{secs / 3600:.1f} h"
    return f"{sign}{secs / 86400:.1f} d"


def badge(status: str) -> str:
    color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
    return f'<span class="pdm-badge" style="background:{color}">{status.upper()}</span>'


def machine_card(row: pd.Series) -> str:
    status = row.get("status", "unknown")
    color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
    health = float(row.get("health_index", 0) or 0)
    due = row.get("maintenance_due_dt")
    return f"""
<div class="pdm-card {status}">
  <h4>{row["machine_id"]} {badge(status)}</h4>
  <div class="sub">{row.get("site", "")} / {row.get("line", "")} · cycle {int(row.get("cycle", 0))} · seen {humanize_delta(row.get("last_seen"))} ago</div>
  <div class="row"><span>Health</span><b>{health:.0f}%</b></div>
  <div class="row"><span>RUL p50 (p10–p90)</span><b>{row["rul_p50"]:.0f} <span class="pdm-muted">({row["rul_p10"]:.0f}–{row["rul_p90"]:.0f})</span></b></div>
  <div class="row"><span>Anomaly</span><b>{row["anomaly_score"]:.2f}</b></div>
  <div class="row"><span>Maintain by</span><b>{humanize_delta(due)}</b></div>
  <div class="pdm-bar"><div style="width:{max(2, health):.0f}%; background:{color}"></div></div>
</div>"""


def require_login() -> None:
    """Minimal shared-secret gate. Production: Cognito/OIDC on the ALB (see docs/runbook.md)."""
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        return
    if st.session_state.get("authed"):
        return
    st.title("🔐 Predictive Maintenance Console")
    with st.form("login"):
        entered = st.text_input("Access code", type="password")
        if st.form_submit_button("Enter") and entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        elif entered:
            st.error("Invalid access code")
    st.stop()
