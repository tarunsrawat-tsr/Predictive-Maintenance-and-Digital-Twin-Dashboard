"""Model bundle: RUL regressor with conformal intervals + anomaly detector.

* RUL point estimate: LightGBM regressor on the rolling-window features, trained against the
  piecewise-linear RUL target (capped at ``rul_cap``) that is standard for C-MAPSS.
* RUL interval (p10/p90): *split conformal prediction*. Hold-out residuals are bucketed by the
  predicted RUL and their 10th/90th percentiles are stored per bucket. At inference the
  interval is ``p50 + offset_lo .. p50 + offset_hi`` for the bucket the prediction falls in.
  This is calibrated by construction and avoids the degenerate behaviour of quantile boosting on
  a capped target. The conservative p10 drives maintenance scheduling.
* Anomaly: Mahalanobis distance of the sensor state against a *healthy* baseline (early-life
  data from the training fleet). Unsupervised, NumPy-only at inference, and it flags off-nominal
  behaviour that does not look like the degradation the RUL model learned. The score is
  normalised so that 1.0 == the 99th percentile of healthy data.

Artifacts are a plain directory so they can be synced to S3 and loaded by the Lambda at
cold start:

    model/
      metadata.json      feature names, window, metrics, training provenance
      rul_p50.txt        LightGBM text model
      interval.json      conformal offsets per predicted-RUL bucket
      anomaly.npz        mean, inverse covariance, scale, threshold
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np

from pdm.features import DEFAULT_WINDOW, FEATURE_NAMES, healthy_vector, window_features

ARTIFACT_FILES = ("metadata.json", "rul_p50.txt", "interval.json", "anomaly.npz")


# ---------------------------------------------------------------------- anomaly detector
@dataclass
class AnomalyDetector:
    mean: np.ndarray
    inv_cov: np.ndarray
    scale: np.ndarray
    threshold: float  # distance at the chosen healthy percentile
    baseline_score: float = 0.75  # median normalised score of healthy data (seeds EWMA filters)

    @classmethod
    def fit(cls, healthy: np.ndarray, percentile: float = 99.0, ridge: float = 1e-3) -> AnomalyDetector:
        scale = healthy.std(axis=0)
        scale[scale == 0] = 1.0
        z = (healthy - healthy.mean(axis=0)) / scale
        cov = np.cov(z, rowvar=False) + ridge * np.eye(z.shape[1])
        inv_cov = np.linalg.inv(cov)
        det = cls(mean=healthy.mean(axis=0), inv_cov=inv_cov, scale=scale, threshold=1.0)
        d = det.distance(healthy)
        det.threshold = float(np.percentile(d, percentile))
        det.baseline_score = float(np.median(d) / det.threshold)
        return det

    def distance(self, x: np.ndarray) -> np.ndarray:
        x = np.atleast_2d(x)
        z = (x - self.mean) / self.scale
        return np.sqrt(np.einsum("ij,jk,ik->i", z, self.inv_cov, z))

    def score(self, x: np.ndarray) -> np.ndarray:
        """Normalised anomaly score: 1.0 == healthy-percentile threshold."""
        return self.distance(x) / self.threshold

    def save(self, path: Path) -> None:
        np.savez(
            path, mean=self.mean, inv_cov=self.inv_cov, scale=self.scale, threshold=self.threshold,
            baseline_score=self.baseline_score,
        )  # fmt: skip

    @classmethod
    def load(cls, path: Path) -> AnomalyDetector:
        with np.load(path) as z:
            return cls(
                mean=z["mean"], inv_cov=z["inv_cov"], scale=z["scale"], threshold=float(z["threshold"]),
                baseline_score=float(z["baseline_score"]) if "baseline_score" in z else 0.75,
            )  # fmt: skip


# ---------------------------------------------------------------------- conformal intervals
@dataclass
class ConformalInterval:
    edges: list[float]  # bucket edges over predicted RUL, len = n_buckets + 1
    lo: list[float]  # per-bucket 10th percentile of residual (y - yhat), usually negative
    hi: list[float]  # per-bucket 90th percentile of residual
    alpha: float = 0.1

    @classmethod
    def fit(
        cls, y_hat: np.ndarray, y_true: np.ndarray, rul_cap: float, n_buckets: int = 6, alpha: float = 0.1
    ):
        edges = np.linspace(0.0, rul_cap, n_buckets + 1).tolist()
        edges[-1] = float("inf")
        resid = y_true - y_hat
        lo, hi = [], []
        for i in range(n_buckets):
            m = (y_hat >= edges[i]) & (y_hat < edges[i + 1])
            r = resid[m] if m.sum() >= 30 else resid  # fall back to global residuals if sparse
            # Anchor the band around the point estimate so p10 <= p50 <= p90 always holds.
            lo.append(min(0.0, float(np.quantile(r, alpha))))
            hi.append(max(0.0, float(np.quantile(r, 1 - alpha))))
        return cls(edges=edges, lo=lo, hi=hi, alpha=alpha)

    def bounds(self, y_hat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        y_hat = np.atleast_1d(y_hat)
        idx = np.clip(np.searchsorted(self.edges, y_hat, side="right") - 1, 0, len(self.lo) - 1)
        lo = y_hat + np.array(self.lo)[idx]
        hi = y_hat + np.array(self.hi)[idx]
        return lo, hi

    def to_dict(self) -> dict:
        return {
            "edges": [e if np.isfinite(e) else 1e9 for e in self.edges],
            "lo": self.lo,
            "hi": self.hi,
            "alpha": self.alpha,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConformalInterval:
        return cls(edges=[float(e) for e in d["edges"]], lo=d["lo"], hi=d["hi"], alpha=d.get("alpha", 0.1))


@dataclass
class Prediction:
    rul_p10: float
    rul_p50: float
    rul_p90: float
    anomaly_score: float
    anomaly_ready: bool = True  # False during burn-in (too little history for a reliable score)


# ---------------------------------------------------------------------- bundle
@dataclass
class ModelBundle:
    booster: lgb.Booster
    interval: ConformalInterval
    anomaly: AnomalyDetector
    metadata: dict = field(default_factory=dict)

    @property
    def window(self) -> int:
        return int(self.metadata.get("window", DEFAULT_WINDOW))

    @property
    def rul_cap(self) -> float:
        return float(self.metadata.get("rul_cap", 125.0))

    @property
    def anomaly_min_history(self) -> int:
        """Cycles of history required before the anomaly score is trusted (burn-in).

        The detector is fitted on full windows; with very short history the rolling-mean
        components collapse onto the raw reading and the distance is inflated (measured on
        C-MAPSS: ~100% false positives at 1 cycle, 4% at 15, 1% at 20)."""
        return int(self.metadata.get("anomaly_min_history", max(1, int(self.window * 0.75))))

    # ------------------------------------------------------------------ inference
    def predict_features(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (p10, p50, p90) arrays, all clipped to [0, rul_cap]."""
        X = np.atleast_2d(X)
        p50 = np.clip(self.booster.predict(X), 0, self.rul_cap)
        lo, hi = self.interval.bounds(p50)
        return np.clip(lo, 0, self.rul_cap), p50, np.clip(hi, 0, self.rul_cap)

    def predict_window(self, window: np.ndarray, cycle: float) -> Prediction:
        """Score one machine given its most recent raw readings (chronological, last = now)."""
        window = np.asarray(window, dtype=float)[-self.window :]
        feats = window_features(window, cycle)
        lo, mid, hi = (float(v[0]) for v in self.predict_features(feats[None, :]))
        ready = window.shape[0] >= self.anomaly_min_history
        score = float(self.anomaly.score(healthy_vector(window)[None, :])[0]) if ready else 0.0
        return Prediction(rul_p10=lo, rul_p50=mid, rul_p90=hi, anomaly_score=score, anomaly_ready=ready)

    # ------------------------------------------------------------------ persistence
    def save(self, directory: Path | str) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(directory / "rul_p50.txt"))
        (directory / "interval.json").write_text(json.dumps(self.interval.to_dict(), indent=2))
        self.anomaly.save(directory / "anomaly.npz")
        meta = {
            "feature_names": FEATURE_NAMES,
            "window": self.window,
            "rul_cap": self.rul_cap,
            **self.metadata,
        }
        (directory / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
        return directory

    @classmethod
    def load(cls, directory: Path | str) -> ModelBundle:
        directory = Path(directory)
        meta = json.loads((directory / "metadata.json").read_text())
        return cls(
            booster=lgb.Booster(model_file=str(directory / "rul_p50.txt")),
            interval=ConformalInterval.from_dict(json.loads((directory / "interval.json").read_text())),
            anomaly=AnomalyDetector.load(directory / "anomaly.npz"),
            metadata=meta,
        )

    @classmethod
    def load_from_s3(cls, s3_uri: str, cache_dir: Path | str | None = None) -> ModelBundle:
        """Download ``s3://bucket/prefix`` into a local cache dir and load it."""
        import boto3

        if not s3_uri.startswith("s3://"):
            raise ValueError(f"not an S3 URI: {s3_uri}")
        bucket, _, prefix = s3_uri[5:].partition("/")
        prefix = prefix.rstrip("/")
        cache_dir = Path(cache_dir or os.path.join(tempfile.gettempdir(), "pdm-model"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        s3 = boto3.client("s3")
        for name in ARTIFACT_FILES:
            s3.download_file(bucket, f"{prefix}/{name}" if prefix else name, str(cache_dir / name))
        return cls.load(cache_dir)

    def upload_to_s3(self, s3_uri: str) -> None:
        import boto3

        bucket, _, prefix = s3_uri[5:].partition("/")
        prefix = prefix.rstrip("/")
        with tempfile.TemporaryDirectory() as tmp:
            self.save(tmp)
            s3 = boto3.client("s3")
            for p in Path(tmp).iterdir():
                s3.upload_file(str(p), bucket, f"{prefix}/{p.name}" if prefix else p.name)


def load_bundle(model_dir: str | Path, model_s3_uri: str = "") -> ModelBundle:
    """Resolve the model location: S3 URI if configured, otherwise a local directory."""
    if model_s3_uri:
        return ModelBundle.load_from_s3(model_s3_uri)
    return ModelBundle.load(model_dir)
