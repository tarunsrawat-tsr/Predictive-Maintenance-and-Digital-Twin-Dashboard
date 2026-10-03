# Model card — RUL regressor + anomaly detector

## Intended use
Rank a fleet of rotating assets by remaining useful life (RUL), surface abnormal behaviour,
and drive maintenance scheduling. Decision support for maintenance planners; not a safety
interlock.

## Training data
NASA C-MAPSS **FD001** (Saxena & Goebel, 2008): 100 run-to-failure turbofan trajectories,
20,631 cycles, one operating condition, one fault mode (HPC degradation). 3 operating
settings + 21 sensors per cycle. 14 sensors carry degradation signal under FD001 and are used
(`s2 s3 s4 s7 s8 s9 s11 s12 s13 s14 s15 s17 s20 s21`).

Re-train with `python ml/train.py --dataset FD00X`; FD002/FD004 (six operating regimes)
will need regime-aware normalisation before the features below are meaningful.

## Features (57)
`cycle` + for each of the 14 sensors over a 20-cycle trailing window: last value, mean,
standard deviation, least-squares slope. Shorter windows (early life / freshly commissioned
machines) use whatever history exists. The same NumPy function runs in training and in the
Lambda (`pdm.features.window_features`, equality covered by tests).

## RUL model
* LightGBM regression, 165 trees (early-stopped on a 25-engine hold-out, then refit on all
  engines), learning rate 0.03, 31 leaves, L2 = 1.
* Target: piecewise-linear RUL capped at **125 cycles** (standard for C-MAPSS — early-life
  wear is not observable, so the model learns "≥ 125" rather than a fake number).
* Prediction clipped to [0, 125].

## Uncertainty
Split-conformal 80 % interval. Residuals on the 25 hold-out engines are bucketed by predicted
RUL (6 buckets); the 10th/90th percentile offset per bucket is stored in `interval.json`
and added to the point estimate at inference. Offsets are anchored so p10 ≤ p50 ≤ p90.
The **p10** drives maintenance deadlines (conservative by design).

## Anomaly model
Mahalanobis distance of the vector `[last values, rolling means]` (28-d) against a healthy
baseline: the first 35 % of life of every training engine, full windows only. Covariance is
ridge-regularised. Score = distance / 99th-percentile-of-healthy, so 1.0 is the "warning"
line and 2.0 "critical". At inference the raw score is EWMA-smoothed (α = 0.3) and not
reported until the machine has ≥ 15 cycles of history (burn-in).

## Evaluation (official FD001 test protocol: last cycle of each of 100 test engines)

| Metric | Value |
| --- | --- |
| RMSE vs true RUL capped at 125 | **14.3 cycles** |
| RMSE vs true (uncapped) RUL | 15.5 cycles |
| NASA PHM08 score (lower is better; late predictions penalised) | **327** |
| Hold-out interval coverage (nominal 80 %) | 80 % |
| Test interval coverage | 75 % |
| Mean interval width | 31 cycles |
| Anomaly false-positive rate per cycle, healthy life (RUL > 125) | ≈ 1 % |
| Share of cycles flagged in last 25 cycles before failure | 89 % |
| Share of cycles flagged 50–75 cycles before failure | 27 % |

For reference, published FD001 results range roughly 12–18 RMSE for CNN/LSTM/transformer
models and 200–500 on the PHM08 score; a gradient-boosted model with hand-crafted window
features is competitive and far cheaper to serve.

Regenerate: `make train` prints the table and writes `artifacts/model/metrics.json`.

## Operating thresholds (defaults, all configurable via env / Terraform)

| Rule | Default |
| --- | --- |
| warning | RUL p50 < 50 cycles **or** anomaly ≥ 1.0 |
| critical | RUL p50 < 20 cycles **or** anomaly ≥ 2.0 |
| hysteresis | status only improves once RUL clears threshold + 8 cycles / anomaly drops below threshold − 0.25 |
| maintenance deadline | p10 RUL − 10 cycles, converted with `cycle_hours` |

## Known limitations
* Simulated data, single fault mode, single operating condition.
* Conformal guarantees are marginal (fleet-average), not per-machine.
* The anomaly detector is linear-Gaussian; multi-modal healthy regimes need a regime
  variable or a different detector (e.g. isolation forest, autoencoder).
* No concept-drift monitoring yet — the S3 data lake + Athena table is where that starts.
