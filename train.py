"""
Train a simple Remaining Useful Life (RUL) model.

What this file does, in order:
    1. Downloads the NASA C-MAPSS dataset if it is missing.
    2. Loads the FD001 training file.
    3. Creates the RUL label for every row.
    4. Creates features from the last 20 cycles (features.py).
    5. Trains a LightGBM regression model.
    6. Compares it with a very simple baseline and prints the RMSE.
    7. Saves the model to model.pkl and the evaluation results
       to the results/ folder (the dashboard's Model page reads them).

Run it with:
    python train.py
"""

import io
import json
import pickle
import urllib.request
import zipfile
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from features import ALL_COLUMNS, RUL_CAP, create_features

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DATA_FOLDER = Path("data")
TRAIN_FILE = DATA_FOLDER / "train_FD001.txt"
MODEL_FILE = Path("model.pkl")
RESULTS_FOLDER = Path("results")

DOWNLOAD_URLS = [
    "https://codeload.github.com/hankroark/Turbofan-Engine-Degradation/zip/refs/heads/master",
    "https://data.nasa.gov/download/ff5v-kuh6/application%2Fzip",
]


# ---------------------------------------------------------------------------
# Step 1: Download the data
# ---------------------------------------------------------------------------
def download_data():
    """Download C-MAPSS into the data/ folder if it is not already there."""
    if TRAIN_FILE.exists():
        print("Dataset already exists. Skipping download.")
        return

    DATA_FOLDER.mkdir(parents=True, exist_ok=True)
    print("Downloading the NASA C-MAPSS dataset...")

    for url in DOWNLOAD_URLS:
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                zip_bytes = response.read()

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zip_file:
                for member in zip_file.namelist():
                    file_name = Path(member).name
                    if file_name.endswith(".txt") and "FD00" in file_name:
                        (DATA_FOLDER / file_name).write_bytes(
                            zip_file.read(member)
                        )

            if TRAIN_FILE.exists():
                print("Dataset downloaded to the data/ folder.")
                return
        except Exception as error:
            print(f"Could not download from one source: {error}")

    raise RuntimeError("Dataset download failed. Check your internet connection.")


# ---------------------------------------------------------------------------
# Step 2: Load the data and create the RUL label
# ---------------------------------------------------------------------------
def load_data():
    """Load train_FD001.txt into a pandas DataFrame."""
    data = pd.read_csv(
        TRAIN_FILE,
        sep=r"\s+",
        header=None,
        names=ALL_COLUMNS,
    )
    return data.sort_values(["unit", "cycle"]).reset_index(drop=True)


def add_rul(data):
    """
    Add the target column: Remaining Useful Life (RUL).

    Example: if an engine lived for 200 cycles, then:
        cycle 150 -> RUL 50
        cycle 200 -> RUL 0

    We cap RUL at 125 because early in life there is very little visible
    wear. The model learns "more than 125" instead of a fake exact number.
    """
    last_cycle = data.groupby("unit")["cycle"].transform("max")
    data["rul"] = last_cycle - data["cycle"]
    data["rul"] = data["rul"].clip(upper=RUL_CAP)
    return data


def rmse_score(true_values, predicted_values):
    """Root Mean Squared Error: average prediction error, in cycles."""
    return float(
        np.sqrt(np.mean((np.array(true_values) - np.array(predicted_values)) ** 2))
    )


# ---------------------------------------------------------------------------
# Step 3: Train the model, compare with a baseline, save everything
# ---------------------------------------------------------------------------
def train_model(features):
    """
    Split by engine, train LightGBM, and evaluate it.

    Important: we split by ENGINE, not by random rows.
    Cycles from the same engine look very similar. If we split randomly,
    the model would see almost the same data in training and testing,
    and the score would be misleading.
    """
    engines = features["unit"].unique()
    rng = np.random.default_rng(42)
    shuffled_engines = rng.permutation(engines)

    test_count = int(len(engines) * 0.25)
    test_engines = shuffled_engines[:test_count]
    train_engines = shuffled_engines[test_count:]

    train_data = features[features["unit"].isin(train_engines)]
    test_data = features[features["unit"].isin(test_engines)]

    feature_columns = [
        column for column in features.columns
        if column not in ["rul", "unit"]
    ]

    # LightGBM builds 300 small decision trees, one after another.
    # Each new tree tries to fix the mistakes of the trees before it.
    params = {
        "objective": "regression",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "verbose": -1,
        "seed": 42,
    }
    lightgbm_train_data = lgb.Dataset(
        train_data[feature_columns], label=train_data["rul"]
    )
    model = lgb.train(params, lightgbm_train_data, num_boost_round=300)

    predictions = model.predict(test_data[feature_columns])
    predictions = np.clip(predictions, 0, RUL_CAP)
    model_rmse = rmse_score(test_data["rul"], predictions)

    # Simple baseline: always predict the average RUL of the training
    # engines. This "dumb" model tells us whether LightGBM is actually
    # learning something useful.
    baseline_predictions = np.full(
        len(test_data), train_data["rul"].mean()
    )
    baseline_rmse = rmse_score(test_data["rul"], baseline_predictions)
    improvement = (baseline_rmse - model_rmse) / baseline_rmse * 100

    print(f"Training engines       : {len(train_engines)}")
    print(f"Testing engines        : {len(test_engines)}")
    print(f"Training rows          : {len(train_data)}")
    print(f"Testing rows           : {len(test_data)}")
    print(f"Baseline RMSE (average): {baseline_rmse:.2f} cycles")
    print(f"LightGBM RMSE          : {model_rmse:.2f} cycles")
    print(f"Improvement            : {improvement:.1f}% better than baseline")

    # ---- Save the evaluation files the dashboard's Model page reads ----
    RESULTS_FOLDER.mkdir(parents=True, exist_ok=True)

    evaluation = pd.DataFrame(
        {
            "actual_rul": test_data["rul"].to_numpy(),
            "predicted_rul": predictions,
        }
    )
    evaluation.to_csv(RESULTS_FOLDER / "evaluation.csv", index=False)

    importance = pd.DataFrame(
        {
            "feature": feature_columns,
            "importance": model.feature_importance(importance_type="gain"),
        }
    ).sort_values("importance", ascending=False)
    importance.to_csv(RESULTS_FOLDER / "feature_importance.csv", index=False)

    metrics = {
        "train_engines": int(len(train_engines)),
        "test_engines": int(len(test_engines)),
        "train_rows": int(len(train_data)),
        "test_rows": int(len(test_data)),
        "baseline_rmse": round(baseline_rmse, 2),
        "model_rmse": round(model_rmse, 2),
        "improvement_percent": round(float(improvement), 1),
        "window": 20,
        "rul_cap": RUL_CAP,
        "number_of_features": len(feature_columns),
    }
    (RESULTS_FOLDER / "metrics.json").write_text(
        json.dumps(metrics, indent=2)
    )

    print("\nTop 10 important features:")
    print(importance.head(10).to_string(index=False))

    return model, feature_columns, metrics


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------
def main():
    print("Step 1/5: Checking the dataset")
    download_data()

    print("\nStep 2/5: Loading the data")
    data = load_data()
    print(f"Rows: {len(data)}, Engines: {data['unit'].nunique()}")

    print("\nStep 3/5: Creating the RUL label")
    data = add_rul(data)

    print("\nStep 4/5: Creating features from the last 20 cycles")
    features = create_features(data)
    print(f"Feature rows: {len(features)}, "
          f"Features per row: {features.shape[1] - 2}")

    print("\nStep 5/5: Training the LightGBM model")
    model, feature_columns, metrics = train_model(features)

    saved_data = {
        "model": model,
        "feature_columns": feature_columns,
        "rmse": metrics["model_rmse"],
    }
    with open(MODEL_FILE, "wb") as file:
        pickle.dump(saved_data, file)

    print(f"\nModel saved to {MODEL_FILE}")
    print(f"Evaluation results saved to the {RESULTS_FOLDER}/ folder")
    print("Next step: start the API with  uvicorn api:app --port 8000")


if __name__ == "__main__":
    main()
