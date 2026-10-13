# Predictive Maintenance & Digital Twin Dashboard

An end-to-end predictive maintenance system that uses machine learning to estimate equipment lifetime, detect anomalies, and plan maintenance.

Built using NASA's C-MAPSS turbofan dataset, the project combines a shared ML pipeline, simulated sensor streaming, AWS services, and a Streamlit dashboard.

## Features

- **RUL prediction:** Estimate remaining useful life using LightGBM, with prediction intervals.
- **Anomaly detection:** Identify unusual sensor behavior against a healthy baseline.
- **Fleet monitoring:** Track machine health, sensor trends, and active alerts.
- **Maintenance planning:** Schedule maintenance based on predicted lifetime and crew capacity.
- **ROI analysis:** Explore potential downtime savings using configurable assumptions.
- **Local and cloud execution:** Run the scoring pipeline locally or deploy it on AWS.

## Architecture

```text
C-MAPSS Simulator
       │ MQTT
       ▼
  AWS IoT Core
       │
       ▼
    Kinesis
       │
       ▼
  Lambda Scorer
   ├── DynamoDB ──► Streamlit Dashboard
   ├── Firehose ──► S3 ──► Athena
   └── SNS ───────► Alerts
```

Terraform provisions the AWS infrastructure. Local adapters allow development and testing without an AWS account.

## Tech Stack

**ML:** Python, LightGBM, Pandas, NumPy  
**Backend:** AWS Lambda, IoT Core, Kinesis, DynamoDB  
**Data:** S3, Firehose, Athena  
**Dashboard:** Streamlit  
**Infrastructure:** Terraform, Docker, AWS ECS Fargate  
**Testing:** Pytest

## Model Results

The model is trained and evaluated on the C-MAPSS FD001 dataset.

| Metric | Result |
|---|---:|
| RUL RMSE | 14.3 cycles |
| NASA PHM08 score | 327 |
| Prediction intervals | 80% conformal interval |

These results apply to FD001 and should not be assumed to generalize to real industrial equipment.

## Run Locally

Requires Python and the dependencies specified by the project.

```bash
make setup
make train
```

Start the simulator:

```bash
make demo-sim
```

In another terminal, launch the dashboard:

```bash
make demo-dash
```

Open [http://localhost:8501](http://localhost:8501).

The simulator replays 12 virtual machines and generates sensor observations every two seconds. Training downloads the dataset and saves the model artifacts locally.

## Deploy to AWS

Configure the Terraform variables using the example file:

```bash
cp infra/terraform/terraform.tfvars.example \
   infra/terraform/terraform.tfvars
```

Then run:

```bash
make train
make infra-ecr
make push
make infra-apply
./scripts/smoke_test.sh
```

See `docs/runbook.md` for deployment and troubleshooting details.

## Testing

Run the test suite with:

```bash
pytest
```

Tests cover feature consistency, model loading, scoring, alerts, maintenance scheduling, storage, and AWS integrations.

## Limitations

- C-MAPSS contains simulated turbofan data, not real industrial telemetry.
- The model is evaluated on FD001 only.
- Maintenance scheduling uses a simple capacity-based approach.
- ROI estimates depend on user-provided assumptions.
- Prediction intervals and anomaly scores are not guarantees of actual equipment failure.

## Dataset

A. Saxena and K. Goebel, *Turbofan Engine Degradation Simulation Data Set*, NASA Prognostics Data Repository, 2008.

The dataset is downloaded during setup and is not committed to the repository.
