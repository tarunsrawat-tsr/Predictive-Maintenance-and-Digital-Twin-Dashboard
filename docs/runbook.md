# Runbook — deploy, operate, tear down

## Prerequisites

* AWS account + credentials with admin (or equivalent) in the target region; `aws` CLI v2.
* Terraform ≥ 1.5, Docker (with buildx; images are built for `linux/amd64`).
* Python 3.11+ for training / local tooling: `make setup`.

## First deployment (≈ 15 minutes)

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars   # edit: alert_email, cidrs, password
make train          # downloads C-MAPSS, trains the bundle -> artifacts/model (≈ 10 s)
make infra-ecr      # terraform init + create the three ECR repositories only
make push           # docker build + push scorer / dashboard / simulator  (tag = git SHA)
make infra-apply    # everything else: IoT, Kinesis, Lambda, DynamoDB, Firehose, ECS, ALB, alarms, budget
```

Then:

1. **Confirm the SNS subscription** email (critical alerts + alarms won't arrive otherwise).
2. Wait ~2 minutes for the ECS services to become healthy, then open `terraform output dashboard_url`.
3. `./scripts/smoke_test.sh` publishes one synthetic message through IoT Core and verifies it was scored.

The simulator Fargate task starts automatically (`simulator_desired_count = 1`). Within a few
minutes the fleet page fills up; machines started mid-life will show RUL immediately and
anomaly scores after their 15-cycle burn-in.

### Running the simulator from a laptop instead

```bash
# in terraform.tfvars: simulator_desired_count = 0 ; make infra-apply
make simulate       # fetches the cert/key from SSM into artifacts/certs and publishes over MQTT/TLS
```

### Loading the model from S3 (hot-swap without rebuilding images)

```bash
make upload-model                               # syncs artifacts/model to s3://<model-bucket>/models/rul/
# terraform.tfvars: model_s3_prefix = "models/rul" ; make infra-apply
```
The Lambda downloads the bundle at cold start; force a refresh by re-applying (the env change
creates a new function version) or by bumping any env var.

## Day-2 operations

| Task | How |
| --- | --- |
| Watch ingest / lag / errors | CloudWatch dashboard `pdm-dev-platform` (`terraform output cloudwatch_dashboard`) |
| Scorer logs | `aws logs tail /aws/lambda/pdm-dev-scorer --follow` |
| Dashboard / simulator logs | `aws logs tail /ecs/pdm-dev-dashboard --follow` |
| Alarms | `pdm-dev-scorer-errors`, `-scorer-iterator-age`, `-scorer-dlq-messages`, `-no-telemetry` → SNS |
| Replay failed batches | Messages in `pdm-dev-scorer-dlq` contain the shard/sequence range; re-drive with the Lambda console or `aws lambda invoke` after fixing the cause |
| Change business thresholds | `health_thresholds` in `terraform.tfvars` → `make infra-apply` (Lambda + dashboard env) |
| Scale ingest | `kinesis_shard_count` (each shard = 1 concurrent scorer); DynamoDB is on-demand |
| Pause costs | `simulator_desired_count = 0`, `dashboard_desired_count = 0` → `make infra-apply` |
| Query history | Athena, database `pdm_dev_datalake`, table `telemetry` (partition projection — no crawler needed) |

Example Athena query — model monitoring, average predicted vs. realised RUL per day:

```sql
SELECT date_trunc('day', from_unixtime(ts/1000)) AS day,
       count(*) AS records,
       avg(rul_p50) AS avg_rul,
       avg(anomaly_score) AS avg_anomaly,
       sum(CASE WHEN status='critical' THEN 1 ELSE 0 END) AS critical_cycles
FROM pdm_dev_datalake.telemetry
WHERE year = year(current_date) AND month = month(current_date)
GROUP BY 1 ORDER BY 1;
```

## Updating the application

```bash
make push IMAGE_TAG=$(git rev-parse --short HEAD)
make infra-apply IMAGE_TAG=$(git rev-parse --short HEAD)
```
ECS uses a deployment circuit breaker with rollback; Lambda updates are atomic.

## Teardown

```bash
make infra-destroy
```
Buckets and ECR repositories are created with `force_destroy` so a single destroy removes
everything (including archived telemetry — export first if you need it). The IoT certificate is
deactivated and deleted by Terraform.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `terraform apply` fails creating the Lambda: image not found | `make push` not run, or tag mismatch | `make push IMAGE_TAG=<tag>` then apply with the same tag |
| Simulator task keeps restarting, logs show TLS errors | certificate inactive / policy missing | `aws iot describe-certificate`; re-apply |
| Fleet page empty | no telemetry yet, or scorer failing | check `-no-telemetry` alarm, Lambda logs, DLQ |
| Anomaly shows 0.00 | burn-in (< 15 cycles of history for that machine) | wait; or lower `anomaly_min_history` when training |
| ALB health checks failing | task still starting (pip import of LightGBM/Plotly takes ~20 s) | grace period is 90 s; check `/ecs/...-dashboard` logs |
| Dashboard slow | too many points in the history slider | lower it; DynamoDB queries are paginated by `limit` |
