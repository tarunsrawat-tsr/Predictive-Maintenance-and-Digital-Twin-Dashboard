# One simple image used by all three services in docker-compose.yml.
# Each service runs a different command from the same image.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000 8501

# Default command (docker-compose overrides this per service).
CMD ["python", "train.py"]
