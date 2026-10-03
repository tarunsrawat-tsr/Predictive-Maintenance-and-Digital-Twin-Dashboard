# Fleet simulator (MQTT publisher). Build from the repository root:
#   docker build -f services/simulator/Dockerfile -t pdm-simulator .
FROM python:3.12-slim

WORKDIR /app
COPY services/simulator/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/pdm /app/src/pdm
COPY services/simulator/main.py /app/services/simulator/main.py
# Bake the dataset in so the container does not need outbound internet at start-up.
COPY data/cmapss /app/data/cmapss

ENV PDM_DATA_DIR=/app/data/cmapss PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "services/simulator/main.py"]
