# Streamlit digital-twin console. Build from the repository root (after `make train`):
#   docker build -f services/dashboard/Dockerfile -t pdm-dashboard .
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/dashboard/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/pdm /app/src/pdm
COPY services/dashboard /app/services/dashboard
COPY .streamlit /app/.streamlit
# Model bundle for the model card + sensor baselines (overridden by PDM_MODEL_S3_URI if set).
COPY artifacts/model /app/artifacts/model

ENV PDM_BACKEND=dynamodb \
    PDM_MODEL_DIR=/app/artifacts/model \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATUS=false

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8501/_stcore/health || exit 1
CMD ["streamlit", "run", "services/dashboard/app.py"]
