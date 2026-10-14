"""
Simulate live machines.

This file pretends that old dataset rows are arriving live from real
machines:

    1. It loads a few engines from the dataset and calls them
       Machine-1, Machine-2, and so on.
    2. Every few seconds, each machine takes its next cycle.
    3. The machine sends its last 20 cycles to the prediction API
       (api.py), exactly like a real machine would send sensor data
       to a server.
    4. The API replies with the predicted Remaining Useful Life (RUL).
    5. The reading and prediction are saved to live_data.db (SQLite).
    6. app.py reads that database and shows the dashboard.

Before running this file:
    1. python train.py
    2. uvicorn api:app --port 8000     (in another terminal)

Then run:
    python simulator.py

Useful options:
    python simulator.py --machines 5 --interval 2
    python simulator.py --machines 3 --interval 0 --steps 10   # quick test
"""

import argparse
import os
import random
import sqlite3
import time
from pathlib import Path

import pandas as pd
import requests

from features import ALL_COLUMNS, SENSORS, WINDOW

# ---------------------------------------------------------------------------
# Simple settings
# ---------------------------------------------------------------------------
DATA_FILE = Path("data/train_FD001.txt")
DATABASE_FILE = Path("live_data.db")
ALL_SENSORS = [f"s{i}" for i in range(1, 22)]

# Where the prediction API is running.
# Inside Docker, docker-compose.yml sets API_URL to the api container.
DEFAULT_API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000/predict")


# ---------------------------------------------------------------------------
# Load the dataset
# ---------------------------------------------------------------------------
def load_dataset():
    if not DATA_FILE.exists():
        raise FileNotFoundError(
            "Dataset not found. Please run train.py first. "
            "It downloads the data."
        )

    data = pd.read_csv(
        DATA_FILE,
        sep=r"\s+",
        header=None,
        names=ALL_COLUMNS,
    )
    return data.sort_values(["unit", "cycle"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Ask the API for a prediction
# ---------------------------------------------------------------------------
def predict_rul(history, api_url):
    """
    Send the last 20 cycles of one machine to the API and return the
    predicted RUL, health, and status from its reply.
    """
    columns_to_send = ["cycle"] + SENSORS
    payload = {"history": history[columns_to_send].to_dict(orient="records")}

    try:
        response = requests.post(api_url, json=payload, timeout=10)
    except requests.ConnectionError:
        raise ConnectionError(
            f"Could not connect to the API at {api_url}. "
            "Please start it first with:  uvicorn api:app --port 8000"
        )

    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------
def create_database():
    """Create a fresh live_data.db file with one simple table."""
    sensor_columns = ", ".join(f"{sensor} REAL" for sensor in ALL_SENSORS)
    connection = sqlite3.connect(DATABASE_FILE)
    connection.execute("DROP TABLE IF EXISTS readings")
    connection.execute(
        f"""
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
            cycle INTEGER,
            rul REAL,
            health REAL,
            status TEXT,
            {sensor_columns}
        )
        """
    )
    connection.commit()
    return connection


def save_reading(connection, machine_id, current_row, prediction):
    """Save one machine reading and the API's prediction."""
    sensor_values = [float(current_row[sensor]) for sensor in ALL_SENSORS]

    columns = ", ".join(ALL_SENSORS)
    placeholders = ", ".join("?" for _ in ALL_SENSORS)
    connection.execute(
        f"""
        INSERT INTO readings
            (machine_id, cycle, rul, health, status, {columns})
        VALUES
            (?, ?, ?, ?, ?, {placeholders})
        """,
        [
            machine_id,
            int(current_row["cycle"]),
            prediction["rul"],
            prediction["health"],
            prediction["status"],
        ]
        + sensor_values,
    )


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Simulate live machines")
    parser.add_argument("--machines", type=int, default=5,
                        help="number of machines to simulate (default: 5)")
    parser.add_argument("--interval", type=float, default=2,
                        help="seconds to wait between cycles (default: 2)")
    parser.add_argument("--steps", type=int, default=0,
                        help="stop after this many steps (default: 0 = run forever)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help="prediction API address")
    args = parser.parse_args()

    print("Loading the dataset...")
    data = load_dataset()

    connection = create_database()
    print(f"Created a fresh database: {DATABASE_FILE}")
    print(f"Prediction API: {args.api_url}")

    # Create the machines. Machine-1 uses engine 1, Machine-2 uses engine 2,
    # and so on. Each machine starts at a different point in its life so the
    # dashboard does not show every machine at the same age.
    rng = random.Random(42)
    machines = []
    engine_numbers = sorted(data["unit"].unique())[: args.machines]

    for number, engine_number in enumerate(engine_numbers, start=1):
        engine_data = data[data["unit"] == engine_number].reset_index(drop=True)
        start_position = rng.randint(0, int(len(engine_data) * 0.6))
        machines.append(
            {
                "name": f"Machine-{number}",
                "data": engine_data,
                "position": start_position,
            }
        )

    print(f"Starting {len(machines)} machines...")
    print("Open the dashboard with:  streamlit run app.py")
    print("Press Ctrl+C to stop.\n")

    step = 0
    try:
        while True:
            step += 1

            for machine in machines:
                engine_data = machine["data"]
                position = machine["position"]

                # When an engine reaches its last cycle, it has "failed".
                # In this simple simulator, we restart it from the
                # beginning. You can explain this as: the machine was
                # repaired and started a new life.
                if position >= len(engine_data):
                    machine["position"] = 0
                    position = 0
                    print(f"{machine['name']} restarted after reaching end of life.")

                history_start = max(0, position - WINDOW + 1)
                history = engine_data.iloc[history_start : position + 1]
                current_row = engine_data.iloc[position]

                prediction = predict_rul(history, args.api_url)
                save_reading(
                    connection, machine["name"], current_row, prediction
                )
                machine["position"] = position + 1

            connection.commit()
            print(f"Step {step}: saved predictions for {len(machines)} machines")

            if args.steps and step >= args.steps:
                break

            if args.interval > 0:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nSimulator stopped.")

    connection.close()
    print(f"Readings are saved in {DATABASE_FILE}")


if __name__ == "__main__":
    main()
