"""Train the RUL model, its conformal intervals, and the anomaly detector on NASA C-MAPSS.

Usage:
    python ml/train.py --dataset FD001 --out artifacts/model [--window 20] [--upload s3://bucket/models/rul]

Outputs a model directory (see pdm.model) plus ``metrics.json`` with hold-out and official
test-set scores (RMSE and the asymmetric NASA PHM08 score).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm.cmapss import add_rul_labels, download, iter_units, load_rul, load_split  # noqa: E402
from pdm.features import FEATURE_NAMES, dataset_features, healthy_vector, window_features  # noqa: E402
from pdm.model import AnomalyDetector, ConformalInterval, ModelBundle  # noqa: E402

LGB_PARAMS = dict(
    objective="regression",
    metric="rmse",
    learning_rate=0.03,
    num_leaves=31,
    min_child_samples=30,
    feature_fraction=0.8,
    bagging_fraction=0.8,
    bagging_freq=1,
    lambda_l2=1.0,
    verbose=-1,
    seed=7,
)


def nasa_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """PHM08 asymmetric score: late predictions (over-estimating RUL) are penalised harder."""
    d = y_pred - y_true
    return float(np.sum(np.where(d < 0, np.exp(-d / 13) - 1, np.exp(d / 10) - 1)))


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="FD001")
    ap.add_argument("--data-dir", default="data/cmapss")
    ap.add_argument("--out", default="artifacts/model")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--rul-cap", type=float, default=125.0)
    ap.add_argument("--rounds", type=int, default=800)
    ap.add_argument(
        "--healthy-frac", type=float, default=0.35, help="early-life fraction used as anomaly baseline"
    )
    ap.add_argument(
        "--anomaly-min-history",
        type=int,
        default=15,
        help="cycles of history before anomaly scoring is trusted",
    )
    ap.add_argument("--upload", default="", help="optional s3://bucket/prefix to upload the bundle to")
    args = ap.parse_args(argv)

    t0 = time.time()
    download(args.data_dir)
    train = load_split("train", args.dataset, args.data_dir)
    test = load_split("test", args.dataset, args.data_dir)
    rul_test = load_rul(args.dataset, args.data_dir)

    # ------------------------------------------------------------------ features
    X, units, _ = dataset_features(train, args.window)
    ordered = np.vstack([rows for _, rows in iter_units(train)])  # same ordering as dataset_features
    y = add_rul_labels(ordered, cap=args.rul_cap)
    print(f"train features: {X.shape}, cap={args.rul_cap}")

    # Group hold-out by engine (no leakage between train/calibration).
    rng = np.random.default_rng(0)
    all_units = np.unique(units)
    val_units = rng.choice(all_units, size=max(1, int(0.25 * len(all_units))), replace=False)
    val_mask = np.isin(units, val_units)
    X_tr, y_tr, X_va, y_va = X[~val_mask], y[~val_mask], X[val_mask], y[val_mask]

    # ------------------------------------------------------------------ RUL model
    dtr = lgb.Dataset(X_tr, y_tr, feature_name=FEATURE_NAMES)
    dva = lgb.Dataset(X_va, y_va, reference=dtr)
    holdout_model = lgb.train(
        LGB_PARAMS, dtr, num_boost_round=args.rounds, valid_sets=[dva],
        callbacks=[lgb.early_stopping(60, verbose=False)],
    )  # fmt: skip
    best_iter = holdout_model.best_iteration or args.rounds
    y_va_hat = np.clip(holdout_model.predict(X_va), 0, args.rul_cap)
    print(f"hold-out: best_iter={best_iter}, rmse={rmse(y_va, y_va_hat):.2f}")

    # Conformal calibration on the hold-out engines (never seen by holdout_model).
    interval = ConformalInterval.fit(y_va_hat, y_va, args.rul_cap)
    lo, hi = interval.bounds(y_va_hat)
    val_cov = float(np.mean((lo <= y_va) & (y_va <= hi)))

    # Refit on all engines with the tuned number of rounds -> shipped model.
    booster = lgb.train(LGB_PARAMS, lgb.Dataset(X, y, feature_name=FEATURE_NAMES), num_boost_round=best_iter)

    # ------------------------------------------------------------------ anomaly detector
    healthy_rows = []
    for _, rows in iter_units(train):
        n = len(rows)
        cutoff = max(args.window, int(n * args.healthy_frac))
        raw = rows[:, 2:]
        for i in range(args.window - 1, cutoff):
            healthy_rows.append(healthy_vector(raw[i + 1 - args.window : i + 1]))
    healthy = np.array(healthy_rows)
    anomaly = AnomalyDetector.fit(healthy)
    print(f"anomaly baseline: {healthy.shape}, threshold={anomaly.threshold:.3f}")

    bundle = ModelBundle(
        booster=booster,
        interval=interval,
        anomaly=anomaly,
        metadata={
            "window": args.window,
            "rul_cap": args.rul_cap,
            "dataset": args.dataset,
            "anomaly_min_history": min(args.anomaly_min_history, args.window),
        },
    )

    # ------------------------------------------------------------------ evaluation
    # Official test protocol: predict at the last available cycle of each test unit.
    Xt_last, y_true = [], []
    for k, (_, rows) in enumerate(iter_units(test)):
        raw = rows[:, 2:]
        Xt_last.append(window_features(raw[-args.window :], rows[-1, 1]))
        y_true.append(rul_test[k])
    Xt_last = np.array(Xt_last)
    y_true = np.array(y_true)
    y_true_c = np.minimum(y_true, args.rul_cap)
    t_lo, t_hat, t_hi = bundle.predict_features(Xt_last)

    # Anomaly sanity check: score should rise toward end of life on the test set.
    early, late = [], []
    for _, rows in iter_units(test):
        raw = rows[:, 2:]
        if len(raw) < 2 * args.window:
            continue
        early.append(anomaly.score(healthy_vector(raw[: args.window])[None, :])[0])
        late.append(anomaly.score(healthy_vector(raw[-args.window :])[None, :])[0])

    importance = sorted(
        zip(FEATURE_NAMES, booster.feature_importance("gain").tolist(), strict=True), key=lambda t: -t[1]
    )[:15]

    metrics = {
        "dataset": args.dataset,
        "window": args.window,
        "rul_cap": args.rul_cap,
        "n_train_rows": int(len(X)),
        "n_train_units": int(len(all_units)),
        "n_calibration_units": int(len(val_units)),
        "best_iteration": int(best_iter),
        "holdout_rmse": rmse(y_va, y_va_hat),
        "holdout_coverage_p10_p90": val_cov,
        "test_rmse_capped": rmse(y_true_c, t_hat),
        "test_rmse_uncapped": rmse(y_true, t_hat),
        "test_nasa_score_capped": nasa_score(y_true_c, t_hat),
        "test_coverage_p10_p90": float(np.mean((t_lo <= y_true_c) & (y_true_c <= t_hi))),
        "test_mean_interval_width": float(np.mean(t_hi - t_lo)),
        "anomaly_score_early_life_mean": float(np.mean(early)),
        "anomaly_score_end_of_trace_mean": float(np.mean(late)),
        "conformal_offsets": interval.to_dict(),
        "top_features_gain": [{"feature": f, "gain": round(g, 1)} for f, g in importance],
        "train_seconds": round(time.time() - t0, 1),
    }
    bundle.metadata.update(
        {"trained_at": datetime.now(UTC).isoformat(), "lgb_params": LGB_PARAMS, "metrics": metrics}
    )

    out = bundle.save(args.out)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(
        json.dumps(
            {k: v for k, v in metrics.items() if k not in ("top_features_gain", "conformal_offsets")},
            indent=2,
        )
    )
    print(f"saved model bundle to {out}")

    if args.upload:
        bundle.upload_to_s3(args.upload)
        print(f"uploaded to {args.upload}")
    return metrics


if __name__ == "__main__":
    main()
