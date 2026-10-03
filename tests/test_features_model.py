from __future__ import annotations

import numpy as np

from pdm.cmapss import add_rul_labels, iter_units
from pdm.features import FEATURE_NAMES, N_FEATURES, healthy_vector, trajectory_features, window_features
from pdm.model import ConformalInterval, ModelBundle
from pdm.schema import INFORMATIVE_SENSORS, RAW_COLUMNS


def test_feature_names_shape():
    assert len(FEATURE_NAMES) == N_FEATURES == 1 + 4 * len(INFORMATIVE_SENSORS)
    assert FEATURE_NAMES[0] == "cycle"
    assert FEATURE_NAMES[1:5] == ["s2_last", "s2_mean", "s2_std", "s2_slope"]


def test_window_features_single_row_has_zero_std_and_slope():
    row = np.arange(len(RAW_COLUMNS), dtype=float)[None, :]
    f = window_features(row, cycle=7)
    assert f[0] == 7
    assert np.all(f[3::4] == 0) and np.all(f[4::4] == 0)
    # last == mean for a single row
    assert np.allclose(f[1::4], f[2::4])


def test_window_features_slope_recovers_linear_trend():
    n = 10
    w = np.zeros((n, len(RAW_COLUMNS)))
    idx = RAW_COLUMNS.index("s2")
    w[:, idx] = 3.0 * np.arange(n) + 1.0
    f = window_features(w, cycle=n)
    assert np.isclose(f[1 + 3], 3.0)  # s2 slope is the 4th feature of the first sensor block
    assert np.isclose(f[1], 3.0 * (n - 1) + 1.0)  # s2 last


def test_trajectory_features_matches_online_windowing(synth_train):
    """Offline (training) features must equal what the online scorer computes incrementally."""
    _, rows = next(iter_units(synth_train))
    W = 10
    offline = trajectory_features(rows, window=W)
    raw = rows[:, 2:]
    window: list[np.ndarray] = []
    for i in range(len(rows)):
        window.append(raw[i])
        window = window[-W:]
        online = window_features(np.array(window), rows[i, 1])
        assert np.allclose(offline[i], online)


def test_rul_labels_capped(synth_train):
    rul = add_rul_labels(synth_train, cap=50)
    assert rul.max() == 50 and rul.min() == 0
    uncapped = add_rul_labels(synth_train, cap=None)
    assert uncapped.max() > 50


def test_conformal_interval_coverage_is_close_to_nominal():
    rng = np.random.default_rng(0)
    y_hat = rng.uniform(0, 125, 5000)
    y = y_hat + rng.normal(0, 10, 5000)
    ci = ConformalInterval.fit(y_hat, y, rul_cap=125)
    lo, hi = ci.bounds(y_hat)
    cov = np.mean((lo <= y) & (y <= hi))
    assert 0.76 <= cov <= 0.84
    round_trip = ConformalInterval.from_dict(ci.to_dict())
    assert np.allclose(round_trip.bounds(y_hat)[0], lo)


def test_bundle_roundtrip_and_prediction(bundle: ModelBundle, tmp_path, synth_train):
    bundle.save(tmp_path / "m")
    loaded = ModelBundle.load(tmp_path / "m")
    _, rows = next(iter_units(synth_train))
    raw = rows[:, 2:]
    p_early = loaded.predict_window(raw[:5], cycle=5)
    p_late = loaded.predict_window(raw[-20:], cycle=len(rows))
    for p in (p_early, p_late):
        assert 0 <= p.rul_p10 <= p.rul_p50 <= p.rul_p90 <= loaded.rul_cap
        assert p.anomaly_score >= 0
    # The synthetic data drifts monotonically -> late life must look worse than early life.
    assert p_late.rul_p50 < p_early.rul_p50
    assert p_late.anomaly_score > p_early.anomaly_score


def test_healthy_vector_excludes_cycle():
    w = np.random.default_rng(0).normal(size=(5, len(RAW_COLUMNS)))
    v1 = healthy_vector(w)
    assert v1.shape == (2 * len(INFORMATIVE_SENSORS),)


def test_anomaly_burn_in(bundle: ModelBundle, synth_train):
    _, rows = next(iter_units(synth_train))
    raw = rows[:, 2:]
    short = bundle.predict_window(raw[:2], cycle=2)
    assert short.anomaly_ready is False and short.anomaly_score == 0.0
    full = bundle.predict_window(raw[: bundle.window], cycle=bundle.window)
    assert full.anomaly_ready is True and full.anomaly_score > 0
