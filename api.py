"""
Simple prediction API.

This is the "backend" of the project. It loads the trained model once
and answers one question over HTTP:

    POST /predict
    Send the last 20 sensor readings of one machine,
    get back its predicted Remaining Useful Life (RUL).

Why have an API at all?
In real company projects, the ML model is usually kept separate from
the dashboard. Any app can then ask the API for a prediction without
knowing anything about machine learning. Here, simulator.py acts like
a real machine sending its readings to this API.

Run it with:
    uvicorn api:app --port 8000

Then open http://localhost:8000/docs to see and test the API in your
browser (FastAPI creates that page automatically).
"""

import pickle
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from features import SENSORS, get_health, get_status, make_feature_row

MODEL_FILE = Path("model.pkl")

app = FastAPI(title="Predictive Maintenance API")

# Load the model once, when the API starts.
# Loading it on every request would be slow.
if MODEL_FILE.exists():
    with open(MODEL_FILE, "rb") as file:
        saved_model = pickle.load(file)
    MODEL = saved_model["model"]
    FEATURE_COLUMNS = saved_model["feature_columns"]
else:
    MODEL = None
    FEATURE_COLUMNS = []


class PredictRequest(BaseModel):
    """What the caller sends: recent readings of one machine.

    Example:
        {
          "history": [
            {"cycle": 41, "s2": 642.1, "s3": 1580.5, ...},
            {"cycle": 42, "s2": 642.4, "s3": 1581.0, ...}
          ]
        }

    Each reading needs "cycle" and the 14 sensors used by the model.
    Up to the last 20 readings are used.
    """

    history: list[dict[str, float]]


class PredictResponse(BaseModel):
    """What the API sends back."""

    rul: float
    health: float
    status: str


@app.get("/health")
def health_check():
    """Simple check that the API is running."""
    return {"status": "ok", "model_loaded": MODEL is not None}


@app.post("/predict", response_model=PredictResponse)
def predict(request: PredictRequest):
    if MODEL is None:
        raise HTTPException(
            status_code=503,
            detail="Model is not loaded. Please run train.py first.",
        )

    if not request.history:
        raise HTTPException(
            status_code=400,
            detail="Please send at least one reading in 'history'.",
        )

    history = pd.DataFrame(request.history)

    needed_columns = ["cycle"] + SENSORS
    missing = [c for c in needed_columns if c not in history.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Readings are missing these values: {missing}",
        )

    features = make_feature_row(history, FEATURE_COLUMNS)
    predicted_rul = float(MODEL.predict(features)[0])
    predicted_rul = max(0.0, min(125.0, predicted_rul))

    return PredictResponse(
        rul=round(predicted_rul, 1),
        health=round(get_health(predicted_rul), 1),
        status=get_status(predicted_rul),
    )
