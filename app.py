"""
Simple Predictive Maintenance Dashboard.

This Streamlit app reads live_data.db, which simulator.py keeps
updating with help from the prediction API (api.py).

It has 3 pages:
    1. Fleet Overview    - see every machine in one table.
    2. Machine Detail    - see one machine's RUL and sensor charts.
    3. Model Performance - see how accurate the model is.

If there is no live database yet (for example, on a public demo
site), the app shows the sample data in assets/sample_data.csv so
visitors can still see how the dashboard looks.

Run order:
    Terminal 1: uvicorn api:app --port 8000
    Terminal 2: python simulator.py
    Terminal 3: streamlit run app.py
"""

import json
import sqlite3
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

DATABASE_FILE = Path("live_data.db")
SAMPLE_FILE = Path("assets/sample_data.csv")
RESULTS_FOLDER = Path("results")
ALL_SENSORS = [f"s{i}" for i in range(1, 22)]

st.set_page_config(
    page_title="Predictive Maintenance Dashboard",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Load data (live SQLite first, sample CSV as fallback)
# ---------------------------------------------------------------------------
def load_all_readings():
    """Return (readings DataFrame, source name)."""
    if DATABASE_FILE.exists():
        try:
            connection = sqlite3.connect(DATABASE_FILE)
            data = pd.read_sql("SELECT * FROM readings", connection)
            connection.close()
            if not data.empty:
                return data, "live"
        except Exception:
            pass

    if SAMPLE_FILE.exists():
        return pd.read_csv(SAMPLE_FILE), "sample"

    return pd.DataFrame(), "none"


def get_latest(data):
    """Most recent reading of every machine, lowest RUL first."""
    latest = data.sort_values("id").groupby("machine_id").tail(1)
    return latest.sort_values("rul").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Page 1: Fleet Overview
# ---------------------------------------------------------------------------
def show_fleet_page(data):
    st.title("Fleet Overview")
    st.write(
        "Every machine's latest predicted Remaining Useful Life (RUL). "
        "Machines with the lowest RUL are shown first because they need "
        "attention first."
    )

    latest = get_latest(data)

    healthy = len(latest[latest["status"] == "Healthy"])
    warning = len(latest[latest["status"] == "Warning"])
    critical = len(latest[latest["status"] == "Critical"])

    column1, column2, column3, column4 = st.columns(4)
    column1.metric("Total Machines", len(latest))
    column2.metric("Healthy", healthy)
    column3.metric("Warning", warning)
    column4.metric("Critical", critical)

    st.subheader("Latest machine status")
    st.write(
        "Healthy = RUL above 50 cycles | "
        "Warning = RUL 20 to 50 cycles | "
        "Critical = RUL below 20 cycles"
    )
    st.dataframe(
        latest[["machine_id", "cycle", "rul", "health", "status"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "machine_id": "Machine",
            "cycle": "Current Cycle",
            "rul": st.column_config.NumberColumn(
                "Predicted RUL (cycles)", format="%.1f"
            ),
            "health": st.column_config.NumberColumn(
                "Health (%)", format="%.1f"
            ),
            "status": "Status",
        },
    )

    st.subheader("Predicted RUL by machine")
    chart = px.bar(
        latest,
        x="machine_id",
        y="rul",
        color="status",
        title="Lower RUL means the machine is closer to failure",
        labels={"machine_id": "Machine", "rul": "Predicted RUL (cycles)"},
        color_discrete_map={
            "Healthy": "#22c55e",
            "Warning": "#f59e0b",
            "Critical": "#ef4444",
        },
    )
    st.plotly_chart(chart, use_container_width=True)


# ---------------------------------------------------------------------------
# Page 2: Machine Detail
# ---------------------------------------------------------------------------
def show_machine_page(data):
    st.title("Machine Detail")

    latest = get_latest(data)
    machine_id = st.selectbox("Choose a machine", latest["machine_id"].tolist())
    machine = latest[latest["machine_id"] == machine_id].iloc[0]

    column1, column2, column3, column4 = st.columns(4)
    column1.metric("Status", machine["status"])
    column2.metric("Predicted RUL", f"{machine['rul']:.1f} cycles")
    column3.metric("Health", f"{machine['health']:.1f}%")
    column4.metric("Current Cycle", int(machine["cycle"]))

    if machine["status"] == "Critical":
        st.error("This machine is critical. Maintenance should be planned soon.")
    elif machine["status"] == "Warning":
        st.warning("This machine is in warning state. Please watch it closely.")
    else:
        st.success("This machine looks healthy.")

    history = (
        data[data["machine_id"] == machine_id]
        .sort_values("id")
        .tail(300)
        .reset_index(drop=True)
    )
    if history.empty:
        return

    # Give each reading a simple number so the chart always moves
    # left to right, even when a machine restarts.
    history["reading"] = range(1, len(history) + 1)

    st.subheader("How the predicted RUL is changing")
    rul_chart = px.line(
        history,
        x="reading",
        y="rul",
        title=f"Predicted RUL for {machine_id}",
        labels={"reading": "Reading Number", "rul": "Predicted RUL (cycles)"},
    )
    st.plotly_chart(rul_chart, use_container_width=True)

    st.subheader("Sensor trends")
    selected_sensors = st.multiselect(
        "Choose sensors to show",
        ALL_SENSORS,
        default=["s2", "s4", "s11", "s14"],
    )

    if selected_sensors:
        sensor_chart = px.line(
            history,
            x="reading",
            y=selected_sensors,
            title=f"Sensor readings for {machine_id}",
            labels={"reading": "Reading Number", "value": "Sensor Value"},
        )
        st.plotly_chart(sensor_chart, use_container_width=True)

    st.subheader("Recent readings")
    columns_to_show = [
        "reading", "cycle", "rul", "health", "status"
    ] + selected_sensors[:4]
    st.dataframe(
        history[columns_to_show].tail(20),
        hide_index=True,
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Page 3: Model Performance
# ---------------------------------------------------------------------------
def show_model_page():
    st.title("Model Performance")
    st.write(
        "This page shows how accurate the model is on engines it never "
        "saw during training. The results come from the files that "
        "train.py saves in the results/ folder."
    )

    metrics_file = RESULTS_FOLDER / "metrics.json"
    if not metrics_file.exists():
        st.warning("No results yet. Please run train.py first.")
        return

    metrics = json.loads(metrics_file.read_text())

    column1, column2, column3, column4 = st.columns(4)
    column1.metric(
        "LightGBM RMSE", f"{metrics['model_rmse']:.2f} cycles"
    )
    column2.metric(
        "Baseline RMSE", f"{metrics['baseline_rmse']:.2f} cycles",
        help="A 'dumb' model that always predicts the average RUL.",
    )
    column3.metric(
        "Improvement", f"{metrics['improvement_percent']:.1f}%"
    )
    column4.metric(
        "Test Engines", metrics["test_engines"],
        help="Engines kept aside and never used in training.",
    )

    st.info(
        f"RMSE means: on average, the prediction is wrong by about "
        f"{metrics['model_rmse']:.0f} cycles. The data was split by "
        f"engine ({metrics['train_engines']} engines for training, "
        f"{metrics['test_engines']} for testing) so similar cycles "
        f"from the same engine cannot appear in both sets."
    )

    evaluation_file = RESULTS_FOLDER / "evaluation.csv"
    if evaluation_file.exists():
        evaluation = pd.read_csv(evaluation_file)
        # Show at most 1,000 points so the chart stays fast.
        if len(evaluation) > 1000:
            evaluation = evaluation.sample(1000, random_state=42)

        st.subheader("Actual RUL vs Predicted RUL")
        st.write(
            "A perfect model would put every dot on the diagonal line."
        )
        chart = px.scatter(
            evaluation,
            x="actual_rul",
            y="predicted_rul",
            opacity=0.5,
            labels={
                "actual_rul": "Actual RUL (cycles)",
                "predicted_rul": "Predicted RUL (cycles)",
            },
        )
        chart.add_shape(
            type="line", x0=0, y0=0, x1=125, y1=125,
            line={"color": "red", "dash": "dash"},
        )
        st.plotly_chart(chart, use_container_width=True)

    importance_file = RESULTS_FOLDER / "feature_importance.csv"
    if importance_file.exists():
        importance = pd.read_csv(importance_file).head(10)
        st.subheader("Top 10 important features")
        st.write(
            "These are the features the model uses most. Most of them "
            "are the average (_mean) sensor values from the last "
            "20 cycles — the general level of a sensor matters more "
            "than a single reading."
        )
        chart = px.bar(
            importance.iloc[::-1],
            x="importance",
            y="feature",
            orientation="h",
            labels={"importance": "Importance", "feature": "Feature"},
        )
        st.plotly_chart(chart, use_container_width=True)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
st.sidebar.title("Predictive Maintenance")
page = st.sidebar.radio(
    "Choose a page",
    ["Fleet Overview", "Machine Detail", "Model Performance"],
)

st.sidebar.divider()
st.sidebar.write("**Simple status rules**")
st.sidebar.write("Healthy: RUL > 50 cycles")
st.sidebar.write("Warning: RUL 20-50 cycles")
st.sidebar.write("Critical: RUL < 20 cycles")

data, source = load_all_readings()

if source == "sample":
    st.sidebar.info(
        "Showing sample data. Run the API and simulator "
        "for live data."
    )

auto_refresh = st.sidebar.checkbox("Auto-refresh every 3 seconds")

if page == "Model Performance":
    show_model_page()
elif data.empty:
    st.title(page)
    st.warning(
        "No data yet. Please run train.py, start the API, and run "
        "simulator.py, then refresh this page."
    )
elif page == "Fleet Overview":
    show_fleet_page(data)
else:
    show_machine_page(data)

# Simple auto-refresh for beginners: wait, then run the app again.
if auto_refresh:
    time.sleep(3)
    st.rerun()
