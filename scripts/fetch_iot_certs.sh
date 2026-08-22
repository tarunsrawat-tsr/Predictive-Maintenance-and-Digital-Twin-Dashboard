#!/usr/bin/env bash
# Pull the gateway certificate + private key (created by Terraform) from SSM so the simulator
# can run from a laptop. Files land in artifacts/certs/ (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
TF_DIR=infra/terraform
mkdir -p artifacts/certs
CERT_PARAM="$(cd "$TF_DIR" && terraform output -json iot_cert_ssm_parameters | python3 -c 'import json,sys; print(json.load(sys.stdin)["certificate_pem"])')"
KEY_PARAM="$(cd "$TF_DIR" && terraform output -json iot_cert_ssm_parameters | python3 -c 'import json,sys; print(json.load(sys.stdin)["private_key"])')"
aws ssm get-parameter --with-decryption --name "$CERT_PARAM" --query Parameter.Value --output text > artifacts/certs/gateway.crt
aws ssm get-parameter --with-decryption --name "$KEY_PARAM"  --query Parameter.Value --output text > artifacts/certs/gateway.key
chmod 600 artifacts/certs/gateway.key
echo ">> wrote artifacts/certs/gateway.{crt,key}"
