"""Rolling-window feature engineering.

The **same** function is used offline (training on C-MAPSS) and online (the scorer Lambda),
which eliminates training/serving skew. It is implemented in plain NumPy so that the Lambda
image does not need pandas.

For each informative sensor we compute, over the most recent ``window`` cycles:
    * last value
    * rolling mean
    * rolling standard deviation
    * slope (least-squares trend per cycle)
plus the current cycle count. Windows shorter than ``window`` (early life) use whatever
history is available, which is exactly what happens on a freshly commissioned machine.
"""

from __future__ import annotations

import numpy as np

from pdm.schema import INFORMATIVE_SENSORS, RAW_COLUMNS

DEFAULT_WINDOW = 20

_SENSOR_IDX = np.array([RAW_COLUMNS.index(s) for s in INFORMATIVE_SENSORS])


def feature_names(sensors: list[str] = INFORMATIVE_SENSORS) -> list[str]:
    names = ["cycle"]
    for s in sensors:
        names += [f"{s}_last", f"{s}_mean", f"{s}_std", f"{s}_slope"]
    return names


FEATURE_NAMES = feature_names()
N_FEATURES = len(FEATURE_NAMES)


def window_features(window: np.ndarray, cycle: float) -> np.ndarray:
    """Compute the feature vector from ``window`` (shape: (n<=W, len(RAW_COLUMNS))).

    ``window`` rows must be in chronological order; the last row is the current reading.
    """
    if window.ndim != 2 or window.shape[0] == 0:
        raise ValueError("window must be a non-empty 2-D array")
    x = window[:, _SENSOR_IDX].astype(float)  # (n, k)
    n = x.shape[0]
    last = x[-1]
    mean = x.mean(axis=0)
    std = x.std(axis=0) if n > 1 else np.zeros_like(last)
    if n > 1:
        t = np.arange(n, dtype=float)
        t -= t.mean()
        slope = (t[:, None] * (x - mean)).sum(axis=0) / (t**2).sum()
    else:
        slope = np.zeros_like(last)
    out = np.empty(1 + 4 * x.shape[1], dtype=float)
    out[0] = cycle
    out[1::4] = last
    out[2::4] = mean
    out[3::4] = std
    out[4::4] = slope
    return out


def trajectory_features(rows: np.ndarray, window: int = DEFAULT_WINDOW) -> np.ndarray:
    """Features for every row of one machine's trajectory ``rows`` (columns: unit, cycle, RAW...).

    Returns an array of shape (len(rows), N_FEATURES). Used for training/evaluation.
    """
    raw = rows[:, 2:]
    cycles = rows[:, 1]
    out = np.empty((len(rows), N_FEATURES), dtype=float)
    for i in range(len(rows)):
        lo = max(0, i + 1 - window)
        out[i] = window_features(raw[lo : i + 1], cycles[i])
    return out


def dataset_features(
    arr: np.ndarray, window: int = DEFAULT_WINDOW
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Features for a whole C-MAPSS array. Returns (X, unit_ids, cycles) aligned row-wise."""
    from pdm.cmapss import iter_units

    xs, units, cycles = [], [], []
    for u, rows in iter_units(arr):
        xs.append(trajectory_features(rows, window))
        units.append(np.full(len(rows), u))
        cycles.append(rows[:, 1])
    return np.vstack(xs), np.concatenate(units), np.concatenate(cycles)


def healthy_vector(window: np.ndarray) -> np.ndarray:
    """Feature subset used by the anomaly detector: last values + rolling means of the
    informative sensors (no cycle count, so the detector is not simply learning "age")."""
    f = window_features(window, 0.0)
    return np.concatenate([f[1::4], f[2::4]])
