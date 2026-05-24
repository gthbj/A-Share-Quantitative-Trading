# Cloud Run Job 容器：跑 strategy.ml_rich_picker.walk_forward
# 配套 PRD_20260525_03
#
# 与 deploy/cloud_run_walk_forward/Dockerfile 几乎相同，仅 ENTRYPOINT 不同。
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir \
       lightgbm \
       google-cloud-bigquery-storage \
       db-dtypes

COPY strategy/ /app/strategy/
COPY utils/ /app/utils/
COPY bigquery_pipeline/ /app/bigquery_pipeline/
COPY account/ /app/account/
COPY engine/ /app/engine/
COPY analytics/ /app/analytics/
COPY data_layer/ /app/data_layer/
COPY config/ /app/config/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 富特征版本：调用 ml_rich_picker.walk_forward
ENTRYPOINT ["python", "-m", "strategy.ml_rich_picker.walk_forward"]
