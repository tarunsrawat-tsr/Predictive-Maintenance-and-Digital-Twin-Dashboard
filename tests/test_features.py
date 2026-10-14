"""
Simple tests for the shared feature code (features.py).

Run them with:
    pytest

These tests do not need the real dataset. They build a tiny fake
dataset with pandas, so they run in a second or two.
"""

import sys
from pathlib import Path

import pandas as pd

# Make sure Python can find features.py and train.py in the main folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features import SENSORS, create_features, make_feature_row
from train import add_rul


def make_fake_data(cycles_per_engine=30):
    """Two fake engines, each with simple increasing sensor values."""
    rows = []
    for unit in [1, 2]:
        for cycle in range(1, cycles_per_engine + 1):
            row = {"unit": unit, "cycle": cycle, "op1": 0, "op2": 0, "op3": 100}
            for i in range(1, 22):
                row[f"s{i}"] = float(cycle * 10 + i)
            rows.append(row)
    return pd.DataFrame(rows)


def test_rul_is_capped_at_125():
    """An engine with a long life should not get RUL above 125."""
    data = make_fake_data(cycles_per_engine=200)
    data = add_rul(data)

    assert data["rul"].max() == 125
    # The last cycle of every engine is the failure point: RUL 0.
    assert data.groupby("unit")["rul"].min().eq(0).all()


def test_create_features_makes_57_features():
    """14 sensors x 4 features + cycle = 57 feature columns."""
    data = add_rul(make_fake_data())
    features = create_features(data)

    feature_columns = [
        c for c in features.columns if c not in ["rul", "unit"]
    ]
    assert len(feature_columns) == 57
    assert "cycle" in feature_columns
    for sensor in SENSORS:
        assert sensor + "_mean" in feature_columns


def test_make_feature_row_matches_training_features():
    """
    The live feature row (used by the API) must be one row with the
    same columns, in the same order, as the training features.
    This is the most important test in the project: training and
    live prediction must create features in exactly the same way.
    """
    data = add_rul(make_fake_data())
    features = create_features(data)
    feature_columns = [
        c for c in features.columns if c not in ["rul", "unit"]
    ]

    one_engine = data[data["unit"] == 1].tail(20)
    row = make_feature_row(one_engine, feature_columns)

    assert row.shape == (1, 57)
    assert list(row.columns) == feature_columns


def test_short_history_has_zero_std_and_trend():
    """A brand-new machine with one reading has no variation or trend."""
    data = add_rul(make_fake_data())
    features = create_features(data)
    feature_columns = [
        c for c in features.columns if c not in ["rul", "unit"]
    ]

    one_reading = data.head(1)
    row = make_feature_row(one_reading, feature_columns)

    for sensor in SENSORS:
        assert row[sensor + "_std"].iloc[0] == 0.0
        assert row[sensor + "_trend"].iloc[0] == 0.0
