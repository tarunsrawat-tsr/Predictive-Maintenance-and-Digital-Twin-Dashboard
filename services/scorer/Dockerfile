# Lambda container image for the streaming scorer.
# Build from the repository root (after `make train`):
#   docker build -f services/scorer/Dockerfile -t pdm-scorer .
FROM public.ecr.aws/lambda/python:3.12

# LightGBM's wheel links against OpenMP.
RUN dnf install -y libgomp && dnf clean all

# Inference needs only numpy + lightgbm + boto3 + pydantic (no pandas/sklearn).
COPY services/scorer/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -t "${LAMBDA_TASK_ROOT}"

COPY src/pdm "${LAMBDA_TASK_ROOT}/pdm"
COPY services/scorer/handler.py "${LAMBDA_TASK_ROOT}/handler.py"

# A model bundle baked into the image is the fallback when PDM_MODEL_S3_URI is not set.
# artifacts/model is produced by `python ml/train.py` (run `make train` before building).
COPY artifacts/model/ "${LAMBDA_TASK_ROOT}/model/"
ENV PDM_MODEL_DIR="/var/task/model" PDM_BACKEND="dynamodb" LOG_LEVEL="INFO"

CMD ["handler.lambda_handler"]
