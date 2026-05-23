FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY . .

ENV FORGE_CONFIG=/app/config.yaml

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8080"]
