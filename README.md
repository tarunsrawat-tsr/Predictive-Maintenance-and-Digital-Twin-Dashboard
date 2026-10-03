# Predictive Maintenance & Digital Twin Dashboard

An AWS-based predictive maintenance system for monitoring industrial equipment, estimating remaining useful life (RUL), detecting anomalies, and planning maintenance.

The project uses NASA's C-MAPSS turbofan degradation dataset to simulate a fleet of machines. Sensor data is published over MQTT and processed through AWS IoT Core, Kinesis, and Lambda. Predictions and machine state are stored in DynamoDB, while the complete telemetry history is archived in S3 and queried through Athena.

A Streamlit dashboard provides a digital-twin view of the fleet, machine-level diagnostics, alerts, maintenance scheduling, and an interactive ROI model.

## Architecture

```text
C-MAPSS replay
      │
      │ MQTT / TLS
      ▼
 AWS IoT Core
      │
      │ IoT Rule
      ▼
   Kinesis
      │
      ▼
 Lambda scorer
      │
      ├──────────────► DynamoDB ──────────► Streamlit dashboard
      │
      ├──────────────► Firehose ──────────► S3 ──► Athena
      │
      └──────────────► SNS
                         │
                         └── Critical alerts / platform alarms / budget alerts
```

The same scoring pipeline is used locally and in AWS. This makes it possible to run the complete application without an AWS account during development.

## Public demo

The quickest way to see the whole system: no AWS account, no dataset download, no training step, no login.

```bash
make setup
make public-demo      # -> http://localhost:8501
```

This provisions its own model (NASA C-MAPSS if it can be downloaded, otherwise a deterministic synthetic fleet), replays a 12-machine fleet through the same scorer the Lambda runs, and serves the console read-only — acknowledgement, the only write path, is disabled. The demo is deterministic, resumes its replay if you restart it, and freezes to a snapshot with `--static`.

### Deploying it

Point **Streamlit Community Cloud** at `streamlit_app.py` in the repository root and deploy; no secrets are required. The root manifests exist for that platform:

| Root file | Purpose |
|---|---|
| `streamlit_app.py` | Entrypoint hosted platforms can pick; enables the demo by default |
| `requirements.txt` | Hosted platforms do not install optional extras, so the dashboard dependencies are listed here as well |
| `packages.txt` | `libgomp1`, the OpenMP runtime LightGBM loads at import |

The same demo runs in the existing dashboard container (`PDM_PUBLIC_DEMO=1`), or behind any reverse proxy. See [docs/public-demo.md](docs/public-demo.md) for every option, the deployment recipes, and the limitations.

## What the system does

For each machine, the system maintains:

- Remaining useful life (RUL)
- RUL uncertainty interval
- Anomaly score
- Health index
- Current machine status
- Maintenance deadline
- Alert history

The dashboard combines these outputs with the machine's recent sensor history to provide both fleet-level and machine-level views.

### Main dashboard pages

| Page | Description |
|---|---|
| Fleet Overview | Fleet health, warning/critical machines, open alerts, upcoming maintenance, RUL ranking, and a plant-floor digital twin |
| Machine Twin | Detailed health information, RUL estimates, anomaly history, sensor trends, and the feature window used by the model |
| Alerts | Active alerts with filtering, acknowledgement, and links to the corresponding machine |
| Maintenance & ROI | Maintenance deadlines, crew-capacity scheduling, downtime estimates, and configurable ROI calculations |
| Model Card | Model metrics, features, thresholds, conformal calibration, and known limitations |

The dashboard automatically refreshes every 5 seconds during the live simulation.

## Model

The predictive model is trained on the NASA C-MAPSS FD001 dataset.

The current implementation uses:

- 100 training engines
- 57 rolling-window features
- LightGBM regression
- 165 trees
- RUL target capped at 125 cycles
- Split-conformal calibration for prediction intervals
- Mahalanobis-distance anomaly detection

The reported FD001 test results are:

| Metric | Result |
|---|---:|
| RUL RMSE | 14.3 cycles |
| NASA PHM08 score | 327 |
| Prediction interval | 80% conformal interval |

The anomaly detector is calibrated against an early-life healthy baseline. Its score is normalized so that a value of 1.0 corresponds approximately to the healthy-life 99th percentile.

Model artifacts and evaluation metrics are saved under:

```text
artifacts/model/
```

Retraining can be performed with:

```bash
python ml/train.py --dataset FD001
```

## Feature pipeline

Feature engineering is implemented once and shared by both training and online inference.

```text
Raw sensor data
      │
      ▼
Rolling window
      │
      ├── Statistical features
      ├── Trend features
      ├── Sensor-derived features
      └── Window-based features
      │
      ▼
57-dimensional feature vector
      │
      ├──► RUL model
      └──► Anomaly detector
```

Using the same feature implementation offline and online helps avoid training/serving inconsistencies.

## Machine health and alerts

The model prediction is combined with rule-based health logic to determine the machine state.

The system uses:

- RUL thresholds
- Anomaly thresholds
- EWMA smoothing
- Hysteresis
- A 15-cycle burn-in period
- Transition-based alert generation

Alerts are generated when a machine changes state rather than on every individual anomalous observation. This reduces repeated notifications while retaining late-life escalations.

The thresholds, safety margin, cycle duration, downtime cost, and crew capacity are configuration parameters and can be supplied through the deployment environment.

## Maintenance scheduling

Maintenance deadlines are calculated from the predicted RUL and a configurable safety margin.

A simple capacity-aware scheduler then assigns maintenance jobs to available crews.

The dashboard displays:

- Machine
- Predicted maintenance deadline
- Required maintenance duration
- Assigned crew
- Scheduling status

The resulting schedule can also be exported as CSV.

This is intentionally a simple scheduling approach rather than a full industrial maintenance optimization system.

## ROI calculation

The dashboard includes an interactive business-case model.

Users can change assumptions such as:

- Number of machines
- Expected failures per machine per year
- Failure detection/capture rate
- Unplanned outage duration
- Planned maintenance duration
- Cost of downtime per hour
- Annual platform cost

For example, under the following assumptions:

```text
20 machines
1.2 failures / machine / year
70% failure capture
24-hour unplanned outage
6-hour planned outage
$15,000 downtime cost / hour
```

the model estimates the resulting avoided downtime cost and compares it with the platform cost.

These figures are scenario calculations, not measured savings from a production deployment.

## AWS infrastructure

The AWS deployment is provisioned using Terraform.

The infrastructure includes:

- AWS IoT Core
- Amazon Kinesis
- AWS Lambda
- Amazon DynamoDB
- Kinesis Data Firehose
- Amazon S3
- AWS Glue / Athena
- Amazon SNS
- Amazon ECR
- Amazon ECS Fargate
- Application Load Balancer
- Systems Manager Parameter Store
- CloudWatch alarms and dashboards
- AWS Budgets

The default deployment targets the Tokyo AWS region.

At the demo scale, the estimated infrastructure cost is approximately $75/month, although actual cost depends on usage, retention, traffic, and AWS pricing.

## Why DynamoDB?

DynamoDB is used for the application's hot operational state.

The dashboard primarily needs access patterns such as:

```text
Get current state for machine X
Get latest N observations for machine X
Get current state for all machines
Get open alerts
```

These are key-value and indexed lookup patterns, making DynamoDB a suitable fit for the dashboard's operational data.

Historical telemetry is handled separately:

```text
Kinesis
   │
   ▼
Firehose
   │
   ▼
S3 data lake
   │
   ▼
Athena
```

This separates the low-latency application state from long-term analytical storage.

## Repository structure

```text
src/pdm/
├── schema.py          MQTT message schema and sensor metadata
├── cmapss.py          C-MAPSS download, parsing and RUL labels
├── features.py        Shared feature engineering
├── model.py           RUL and anomaly models
├── health.py          Health state and alert logic
├── scoring.py         Online scoring pipeline
├── simulator.py       Fleet replay simulator (resumable replay cursor)
├── synthetic.py       Deterministic fallback fleet for offline demos
├── demo.py            Public-demo bootstrap: model, seeded store, live loop
└── storage/           DynamoDB and SQLite adapters

ml/
└── train.py           Training and evaluation

services/
├── scorer/            Lambda scorer
├── simulator/         MQTT fleet simulator
└── dashboard/         Streamlit application

infra/
└── terraform/         AWS infrastructure

scripts/
├── demo.py            Two-process local demo (simulator + dashboard)
├── public_demo.py     One-command, read-only public demo
├── build_and_push.sh
├── fetch_iot_certs.sh
└── smoke_test.sh

tests/                 Automated tests

streamlit_app.py       Hosted entrypoint (Streamlit Community Cloud)
requirements.txt       Dependencies for hosted deployments
packages.txt           System packages for hosted deployments (libgomp1)

docs/
├── architecture.md
├── business-case.md
├── model-card.md
├── public-demo.md
└── runbook.md
```

## Run locally

An AWS account is not required for the local demo.

> If you only want the dashboard with plausible data in it, use `make public-demo` above — it skips the dataset and training steps entirely.

### 1. Install dependencies and train the model

```bash
make setup
make train
```

Training downloads the C-MAPSS dataset and creates the model artifacts.

### 2. Start the simulator

In one terminal:

```bash
make demo-sim
```

The simulator replays 12 virtual machines, generating one cycle every two seconds. It also performs an initial backfill so that the dashboard has historical data when it starts.

### 3. Start the dashboard

In another terminal:

```bash
make demo-dash
```

Then open:

```text
http://localhost:8501
```

The local implementation uses the same scoring library as the AWS Lambda deployment, but replaces AWS services with local storage/adapters where appropriate.

## Deploy to AWS

Configure the Terraform variables first:

```bash
cp infra/terraform/terraform.tfvars.example \
   infra/terraform/terraform.tfvars
```

Then build the model and infrastructure:

```bash
make train
make infra-ecr
make push
make infra-apply
```

Run the smoke test:

```bash
./scripts/smoke_test.sh
```

The dashboard URL can be retrieved with:

```bash
terraform -chdir=infra/terraform output dashboard_url
```

Additional deployment and troubleshooting information is available in:

```text
docs/runbook.md
```

## Testing

The project currently contains 50 automated tests covering:

- Online/offline feature parity
- Model serialization and loading
- Health-state transitions
- Alert generation
- Scheduler behavior
- ROI calculations
- Lambda event handling
- End-to-end scoring
- SQLite storage
- Mocked DynamoDB, Firehose and SNS interactions
- Dashboard rendering, including the read-only public demo
- Public-demo provisioning, synthetic fallback, replay resume and determinism

The goal is to test the core scoring logic independently from AWS so that most development can be performed locally.

## Dataset

The project uses:

> A. Saxena and K. Goebel, "Turbofan Engine Degradation Simulation Data Set," NASA Prognostics Data Repository, 2008.

The dataset is downloaded during setup and is not included in the repository.

## Limitations

This project is a reference implementation and simulation rather than a production predictive-maintenance deployment.

In particular:

- C-MAPSS is simulated turbofan data and does not represent a specific industrial plant.
- The model is evaluated on FD001 and should not be assumed to generalize to other equipment without validation.
- The maintenance scheduler is a greedy capacity-based scheduler rather than a full optimization model.
- ROI results depend entirely on the assumptions entered by the user.
- The anomaly detector depends on the selected healthy-life baseline.
- AWS cost estimates vary with workload and region.
- Prediction intervals describe model uncertainty under the calibration procedure; they are not guarantees of the actual failure time.

## Project goals

The project is intended to demonstrate an end-to-end predictive-maintenance architecture rather than only an ML model.

It combines:

```text
Machine learning
       +
Streaming data
       +
AWS cloud architecture
       +
Digital twin visualization
       +
Maintenance planning
       +
Business impact analysis
```

The main design objective is to connect the model's prediction to an operational decision: not just "this machine has an RUL of 32 cycles," but also "when should maintenance be scheduled, what is the current machine state, and what is the estimated business impact?"
