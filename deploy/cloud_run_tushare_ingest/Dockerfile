FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY data_ingestion/ /app/data_ingestion/
COPY config/tushare_to_gcs.yaml /app/config/tushare_to_gcs.yaml

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "data_ingestion.tushare_to_gcs", "run", "--config", "/app/config/tushare_to_gcs.yaml"]

