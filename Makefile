# Predictive Maintenance & Digital Twin -- developer / operator entry points
#
#   make setup          install python deps for local dev + tests
#   make data           download NASA C-MAPSS
#   make train          train the model bundle -> artifacts/model
#   make test           unit + integration tests (moto-mocked AWS)
#   make demo           run simulator+scorer+dashboard locally (no AWS)
#
#   make infra-init     terraform init
#   make infra-ecr      create only the ECR repositories (first deploy)
#   make push           build + push the three images to ECR
#   make infra-apply    terraform apply (everything)
#   make deploy         = infra-ecr + push + infra-apply
#   make upload-model   copy artifacts/model to the model bucket (then set model_s3_prefix)
#   make iot-certs      fetch gateway cert/key from SSM for running the simulator locally
#   make simulate       run the simulator from this machine against IoT Core
#   make infra-destroy  tear everything down

SHELL := /bin/bash
PY ?= python3
TF_DIR := infra/terraform
TF ?= terraform
AWS_REGION ?= $(shell cd $(TF_DIR) 2>/dev/null && $(TF) output -raw aws_region 2>/dev/null || echo ap-northeast-1)
IMAGE_TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
SERVICES := scorer dashboard simulator

.PHONY: setup data train test lint demo demo-sim demo-dash infra-init infra-ecr infra-plan infra-apply infra-destroy push deploy upload-model iot-certs simulate clean

setup:
	$(PY) -m pip install -e ".[dev,train,dashboard,simulator]"

data:
	$(PY) -c "from pdm.cmapss import download; print(download('data/cmapss'))"

train: data
	$(PY) ml/train.py --dataset FD001 --out artifacts/model

test:
	$(PY) -m pytest -q

lint:
	ruff check . && ruff format --check .

# ---------------------------------------------------------------- local demo (no AWS)
demo:
	@echo ">> starting simulator+scorer in the background and the dashboard in the foreground"
	@$(PY) scripts/demo.py --machines 12 --interval 2 --warmup 150 --reset & echo $$! > .demo.pid; \
	 PDM_BACKEND=local PDM_DEMO_MODE=1 $(PY) -m streamlit run services/dashboard/app.py; \
	 kill $$(cat .demo.pid) 2>/dev/null; rm -f .demo.pid

demo-sim:
	$(PY) scripts/demo.py --machines 12 --interval 2 --warmup 150 --reset

demo-dash:
	PDM_BACKEND=local PDM_DEMO_MODE=1 $(PY) -m streamlit run services/dashboard/app.py

# ---------------------------------------------------------------- AWS
infra-init:
	cd $(TF_DIR) && $(TF) init

infra-ecr: infra-init
	cd $(TF_DIR) && $(TF) apply -auto-approve \
	  -target='aws_ecr_repository.svc["scorer"]' \
	  -target='aws_ecr_repository.svc["dashboard"]' \
	  -target='aws_ecr_repository.svc["simulator"]'

infra-plan:
	cd $(TF_DIR) && $(TF) plan -var image_tag=$(IMAGE_TAG)

infra-apply:
	cd $(TF_DIR) && $(TF) apply -var image_tag=$(IMAGE_TAG)

infra-destroy:
	cd $(TF_DIR) && $(TF) destroy

push: train
	IMAGE_TAG=$(IMAGE_TAG) ./scripts/build_and_push.sh $(SERVICES)

deploy: infra-ecr push infra-apply

upload-model: train
	$(eval BUCKET := $(shell cd $(TF_DIR) && $(TF) output -raw model_bucket))
	aws s3 sync artifacts/model s3://$(BUCKET)/models/rul/ --delete
	@echo ">> now set model_s3_prefix = \"models/rul\" in terraform.tfvars and re-apply"

iot-certs:
	./scripts/fetch_iot_certs.sh

simulate: iot-certs
	$(eval ENDPOINT := $(shell cd $(TF_DIR) && $(TF) output -raw iot_endpoint))
	IOT_CERT_PATH=artifacts/certs/gateway.crt IOT_KEY_PATH=artifacts/certs/gateway.key \
	$(PY) services/simulator/main.py --host $(ENDPOINT) --machines 10 --interval 5

clean:
	rm -rf artifacts/pdm_local.sqlite* .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
