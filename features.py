"""
Shared feature engineering.

Both training (train.py) and live prediction (api.py, through
simulator.py) use the functions in this file.

Why does this matter?
If training created features one way and live prediction created them
another way, the model would receive different inputs in real use and
its predictions would become unreliable. Keeping ONE copy of this
logic means training and prediction can never drift apart.

For each of 14 useful sensors we create 4 features from the last
20 cycles:
    _last  : current sensor value
    _mean  : average of the last 20 cycles
    _std   : variation in the last 20 cycles
    _trend : current value minus the value 20 cycles ago

14 sensors x 4 features + cycle = 57 features.
"""

import pandas as pd

WINDOW = 20       # How many past cycles we look at.
RUL_CAP = 125     # RUL above 125 is treated as "healthy / more than 125".

# In FD001 these 14 sensors change as the engine gets older.
# The other 7 sensors are almost constant, so they do not help.
SENSORS = [
    "s2", "s3", "s4", "s7", "s8", "s9", "s11",
    "s12", "s13", "s14", "s15", "s17", "s20", "s21",
]

# Every row in the C-MAPSS files has these 26 columns.
ALL_COLUMNS = (
    ["unit", "cycle", "op1", "op2", "op3"]
    + [f"s{i}" for i in range(1, 22)]
)


def create_features(data):
    """
    Create features for the whole training dataset.

    Input : DataFrame with unit, cycle, sensors, and a rul column.
    Output: DataFrame with 57 feature columns plus rul and unit.
    """
    all_engines = []

    for _, engine in data.groupby("unit"):
        engine = engine.sort_values("cycle")
        features = pd.DataFrame(index=engine.index)
        features["cycle"] = engine["cycle"]

        for sensor in SENSORS:
            values = engine[sensor]
            features[sensor + "_last"] = values
            features[sensor + "_mean"] = (
                values.rolling(WINDOW, min_periods=1).mean()
            )
            features[sensor + "_std"] = (
                values.rolling(WINDOW, min_periods=1).std().fillna(0)
            )
            features[sensor + "_trend"] = (
                values - values.shift(WINDOW - 1)
            ).fillna(0)

        features["rul"] = engine["rul"]
        features["unit"] = engine["unit"]
        all_engines.append(features)

    return pd.concat(all_engines).reset_index(drop=True)


def make_feature_row(history, feature_columns):
    """
    Create ONE feature row from up to 20 recent readings.

    This is the live version of create_features(): the API receives
    the last 20 readings of one machine and needs exactly one row of
    features to give to the model.

    `history` is a DataFrame where each row has cycle + sensor values.
    """
    history = history.tail(WINDOW)
    current = history.iloc[-1]
    features = {"cycle": float(current["cycle"])}

    for sensor in SENSORS:
        values = history[sensor]
        features[sensor + "_last"] = float(values.iloc[-1])
        features[sensor + "_mean"] = float(values.mean())
        features[sensor + "_std"] = (
            float(values.std()) if len(values) > 1 else 0.0
        )

        if len(values) >= WINDOW:
            features[sensor + "_trend"] = float(
                values.iloc[-1] - values.iloc[-WINDOW]
            )
        else:
            features[sensor + "_trend"] = 0.0

    return pd.DataFrame([features], columns=feature_columns)


def get_status(rul):
    """Turn a predicted RUL number into a simple status word."""
    if rul < 20:
        return "Critical"
    if rul < 50:
        return "Warning"
    return "Healthy"


def get_health(rul):
    """Convert RUL into a simple health percentage from 0 to 100."""
    health = (rul / RUL_CAP) * 100
    return max(0.0, min(100.0, health))
