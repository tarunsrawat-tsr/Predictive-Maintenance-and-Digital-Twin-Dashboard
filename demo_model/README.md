# Committed demo model bundle

A 490 KB model bundle, trained on the real NASA C-MAPSS FD001 dataset, checked in so a hosted
deployment can start without training.

## Why it is here

Hosted platforms (Streamlit Community Cloud and similar) give the app an ephemeral filesystem:
`artifacts/model/` is empty on every cold start. Provisioning the demo therefore had to fetch
the 15 MB dataset and retrain — a ~7.5 s CPU burst *per cold start*, on a shared free tier that
throttles apps for exactly that. Copying this bundle into place instead costs a file copy, and
halves cold-start CPU to ≈4 s (the remainder is data loading and seeding the store).

Shipping a bundle also keeps the demo honest. If the fallback were "train on synthetic data",
the Model Card would report a test RMSE near 1.0 cycle, which is a meaningless number dressed
up as a result. These artifacts report the metrics the README documents (test RMSE 14.3 capped,
NASA PHM08 score 327).

## Contents

| File | Purpose |
|---|---|
| `metadata.json` | Feature names, window, RUL cap, and the evaluation metrics the Model Card renders |
| `rul_p50.txt` | LightGBM model (text format) |
| `interval.json` | Split-conformal p10/p90 offsets per predicted-RUL bucket |
| `anomaly.npz` | Mahalanobis baseline: mean, inverse covariance, scale, threshold |
| `metrics.json` | The same metrics as written by `make train` (kept for parity) |

## Provenance and regeneration

Produced by `make train` (`ml/train.py --dataset FD001`), which is deterministic given the
fixed seed in `LGB_PARAMS`. To regenerate after changing the feature set or window size:

```bash
make train
cp artifacts/model/{metadata.json,rul_p50.txt,interval.json,anomaly.npz,metrics.json} demo_model/
```

Provenance is recorded inside `metadata.json` (`trained_at`, `lgb_params`, and the full metrics
block) and is surfaced by the dashboard's Model Card page and the public-demo banner.

`pdm.demo.ensure_bundle` prefers, in order: an existing bundle in `PDM_MODEL_DIR`, then this
directory, then training. Deleting this directory is safe — the demo falls back to training —
but a hosted deployment will then pay the cold-start cost on every restart.
