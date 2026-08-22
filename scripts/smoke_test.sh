#!/usr/bin/env bash
# Post-deploy smoke test: publish one message through IoT Core and check it was scored.
set -euo pipefail
cd "$(dirname "$0")/.."
TF_DIR=infra/terraform
REGION="${AWS_REGION:-$(aws configure get region || echo ap-northeast-1)}"
STATE_TABLE="$(cd "$TF_DIR" && terraform output -json dynamodb_tables | python3 -c 'import json,sys; print(json.load(sys.stdin)["machine_state"])')"
DASH="$(cd "$TF_DIR" && terraform output -raw dashboard_url)"

echo ">> publishing a synthetic message via iot-data (uses your AWS credentials, not the device cert)"
PAYLOAD="$(python3 - <<'PY'
import json, sys, os
sys.path.insert(0, "src")
from pdm.simulator import FleetSimulator
sim = FleetSimulator(n_machines=1, machine_prefix="SMOKE", data_dir="data/cmapss")
print(sim.step()[0].model_dump_json())
PY
)"
aws iot-data publish --region "$REGION" --topic "factory/nagoya/L0/SMOKE-001/telemetry" --payload "$(echo -n "$PAYLOAD" | base64 -w0)" --cli-binary-format raw-in-base64-out >/dev/null 2>&1 \
  || aws iot-data publish --region "$REGION" --topic "factory/nagoya/L0/SMOKE-001/telemetry" --payload "$PAYLOAD" --cli-binary-format raw-in-base64-out

echo ">> waiting for the scorer (batch window + cold start)"
for i in $(seq 1 12); do
  sleep 10
  if aws dynamodb get-item --region "$REGION" --table-name "$STATE_TABLE" --key '{"machine_id":{"S":"SMOKE-001"}}' --query Item.status.S --output text 2>/dev/null | grep -qE 'healthy|warning|critical'; then
    echo ">> scored: SMOKE-001 is present in $STATE_TABLE"
    echo ">> dashboard: $DASH"
    curl -fsS -o /dev/null -w "dashboard health: %{http_code}\n" "$DASH/_stcore/health" || true
    exit 0
  fi
done
echo "!! SMOKE-001 not scored after 2 minutes -- check CloudWatch logs for the scorer Lambda" >&2
exit 1
