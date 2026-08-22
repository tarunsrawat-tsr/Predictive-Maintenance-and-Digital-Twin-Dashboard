# Predictive Maintenance & Digital Twin Dashboard

**Industry X / IoT reference implementation on AWS.** Sensor streams from a simulated factory
fleet (NASA C‑MAPSS turbofan degradation data) flow over **MQTT into AWS IoT Core → Kinesis →
a Lambda scorer** that predicts **remaining useful life (RUL)** with calibrated uncertainty and
flags **anomalies**, lands results in **DynamoDB + an S3/Athena data lake**, and powers a live
**Streamlit digital‑twin console** with machine health, alerts, a capacity‑aware maintenance
schedule and an ROI calculator. Everything is provisioned with **Terraform**.

```
C-MAPSS replay ─MQTT/TLS─▶ IoT Core ─rule─▶ Kinesis ─▶ Lambda scorer ─┬─▶ DynamoDB ─▶ Streamlit console (ECS Fargate + ALB)
                                                                       ├─▶ Firehose ─▶ S3 data lake ─▶ Athena
                                                                       └─▶ SNS (critical alerts, platform alarms, budget)
```

| | |
| --- | --- |
| **Business problem** | Unplanned equipment downtime. Leadership wants to know *when* machines will fail and *whether the investment pays off*. |
| **Answer** | Per-machine RUL (median + 80 % band), anomaly score, health index and a just-in-time maintenance plan; an interactive ROI model using the plant's own cost assumptions. |
| **Model quality** | RMSE **14.3 cycles**, NASA PHM08 score **327** on the official FD001 test protocol; 80 % conformal intervals; anomaly detector with ≈ 1 % false-positive rate in healthy life and 89 % hit rate in the final 25 cycles. |
| **Cloud** | AWS, Tokyo region by default. ≈ $75/month at demo scale, fully serverless hot path, Budgets alarm included. |
| **Tests** | 30 tests: feature parity online/offline, model round-trip, health rules, end-to-end pipeline on SQLite **and** moto-mocked DynamoDB/Firehose/SNS, Lambda handler, headless render of every dashboard page. |

---

## Dashboard tour

| Page | What you see |
| --- | --- |
| **Fleet overview** | KPIs (fleet health, critical/warning counts, open alerts, next maintenance, downtime cost avoided), a plant-floor **digital twin** (one lane per production line, hexagon per machine, colour = status, size = health), RUL ranking with uncertainty bars, asset cards, latest alerts. Auto-refreshes every 5 s. |
| **Machine twin** | Health gauge, RUL p10/p50/p90, anomaly score, maintenance deadline, *why this status*. A turbofan cross-section with live sensor read-outs coloured by deviation from the healthy baseline (z-score). RUL trajectory with conformal band, anomaly timeline, sensor trends, the exact feature window the model saw, alert history. |
| **Alerts** | Inbox with filters, multi-row select → **acknowledge** (writes back to DynamoDB), jump to the machine twin. Alerts fire on status *transitions* only (hysteresis + EWMA + burn-in keep them quiet). |
| **Maintenance & ROI** | Deadlines from conservative RUL, greedy **crew-capacity scheduler** (Gantt + table + CSV export), and the **business case**: net annual benefit, ROI, payback, downtime hours avoided, all live-editable. |
| **Model card** | Metrics, feature importance, conformal offsets, thresholds, limitations — the page a reviewer asks for. |

Run it locally in one minute (no AWS account needed — the same scorer code is fed in‑process):

```bash
make setup && make train       # installs deps, downloads C-MAPSS, trains (≈ 10 s)
make demo-sim                  # terminal 1: 12 machines, 1 cycle every 2 s, 150 cycles of back-fill
make demo-dash                 # terminal 2: http://localhost:8501
```

---

## What's in the box

```
src/pdm/                 shared library
  schema.py              MQTT contract (pydantic) + sensor metadata
  cmapss.py              dataset download/parse, RUL labels
  features.py            rolling-window features — one NumPy function used by training AND the Lambda
  model.py               LightGBM RUL + split-conformal intervals + Mahalanobis anomaly; S3 load/save
  health.py              health index, status rules w/ hysteresis, alert transitions, scheduler, ROI
  scoring.py             StreamScorer: micro-batch → features → predictions → state/alerts/sinks
  simulator.py           fleet replay engine (overhauls failed units, virtual clock)
  storage/               TelemetryStore protocol; DynamoDB and SQLite adapters
ml/train.py              training + official-test evaluation → artifacts/model (+ metrics.json)
services/scorer/         Lambda (container image): Kinesis → StreamScorer
services/simulator/      MQTT/TLS publisher (X.509, certifi CA bundle), Fargate or laptop
services/dashboard/      Streamlit console (views/, charts.py, common.py)
infra/terraform/         IoT Core, Kinesis, Firehose, S3, Glue/Athena, DynamoDB, Lambda+ESM+DLQ,
                         SNS, VPC, ALB, ECS Fargate ×2, SSM secrets, CloudWatch alarms/dashboard, Budgets
scripts/                 demo.py, build_and_push.sh, fetch_iot_certs.sh, smoke_test.sh
tests/                   see above
docs/                    architecture.md · business-case.md · model-card.md · runbook.md
```

## Deploy to AWS

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars   # alert_email, cidrs, password
make train            # model bundle baked into the images
make infra-ecr        # create ECR repos
make push             # build + push scorer / dashboard / simulator images (linux/amd64)
make infra-apply      # everything else
./scripts/smoke_test.sh
terraform -chdir=infra/terraform output dashboard_url
```

Full runbook (day-2 ops, Athena queries, troubleshooting, teardown): [`docs/runbook.md`](docs/runbook.md).

## Design decisions worth knowing

* **DynamoDB, not Timestream.** Timestream for LiveAnalytics has been closed to new AWS customers since June 2025 and its successor (Timestream for InfluxDB) is a VPC-bound instance. The console's access patterns (latest *N* per machine, current state of all machines, open alerts) are key lookups; DynamoDB serves them serverlessly with TTL for the hot window while Firehose → S3 → Athena keeps the full history. See [`docs/architecture.md`](docs/architecture.md).
* **Stateless Lambda, stateful features.** Each batch rebuilds the per-machine window from DynamoDB, so the scorer survives redeploys and reshards without a stream-processing cluster. Kinesis partitioning by `machine_id` preserves ordering.
* **Conformal intervals instead of quantile boosting.** The piecewise-linear RUL target makes quantile objectives degenerate (most rows sit at the cap); split-conformal offsets are calibrated by construction.
* **Alert hygiene is designed, not hoped for.** Transition-based alerts, hysteresis, EWMA-smoothed anomaly score and a 15-cycle burn-in cut alert volume ~5× on the demo fleet without losing a single late-life escalation.
* **Business rules are configuration.** RUL/anomaly thresholds, safety margin, cycle duration, downtime cost, crews — all env vars, set from Terraform, shared by scorer and dashboard.

## Model

Trained on C‑MAPSS FD001 (100 engines). 57 window features → LightGBM (165 trees) on a RUL target capped at 125 → conformal p10/p90. Anomaly: Mahalanobis distance to an early-life baseline, normalised so 1.0 = healthy p99. Details, numbers and limitations: [`docs/model-card.md`](docs/model-card.md). Retrain with `python ml/train.py --dataset FD001` (metrics are printed and saved to `artifacts/model/metrics.json`).

## Business case

The dashboard's ROI page and [`docs/business-case.md`](docs/business-case.md) walk through the model: each failure caught early converts a long unplanned outage into a short planned one. With 20 machines, 1.2 failures/machine-year, 70 % capture, 24 h → 6 h outages at $15k/h, the net benefit is ≈ $4.5 M/year against a $60 k platform — and still ≈ $390 k/year at a tenth of that downtime cost.

## Dataset

A. Saxena and K. Goebel (2008). *Turbofan Engine Degradation Simulation Data Set*, NASA Prognostics Data Repository. Downloaded automatically by `make data`; not committed.

## License

MIT
