#!/usr/bin/env bash
# Build the service images and push them to the ECR repositories created by Terraform.
#   IMAGE_TAG=abc123 ./scripts/build_and_push.sh scorer dashboard simulator
set -euo pipefail

cd "$(dirname "$0")/.."
TF_DIR=infra/terraform
IMAGE_TAG="${IMAGE_TAG:-latest}"
SERVICES=("$@")
[[ ${#SERVICES[@]} -eq 0 ]] && SERVICES=(scorer dashboard simulator)

if [[ ! -f artifacts/model/metadata.json ]]; then
  echo "artifacts/model is missing -- run 'make train' first" >&2
  exit 1
fi

REPOS_JSON="$(cd "$TF_DIR" && terraform output -json ecr_repositories)"
REGION="$(cd "$TF_DIR" && terraform output -raw aws_region)"
REGISTRY="$(echo "$REPOS_JSON" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin).values())).split("/")[0])')"

echo ">> logging in to $REGISTRY"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

for svc in "${SERVICES[@]}"; do
  repo="$(echo "$REPOS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['$svc'])")"
  echo ">> building $svc -> $repo:$IMAGE_TAG"
  # Lambda + Fargate task definitions are declared for x86_64.
  docker build --platform linux/amd64 -f "services/$svc/Dockerfile" -t "$repo:$IMAGE_TAG" -t "$repo:latest" .
  docker push "$repo:$IMAGE_TAG"
  docker push "$repo:latest"
done
echo ">> done. deploy with: make infra-apply IMAGE_TAG=$IMAGE_TAG"
