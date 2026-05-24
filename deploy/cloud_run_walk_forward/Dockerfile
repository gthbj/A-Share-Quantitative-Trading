# Cloud Run Job 容器：跑 strategy.ml_multi_horizon_picker.walk_forward
# 配套 PRD_20260524_14
FROM python:3.11-slim

WORKDIR /app

# 系统依赖（lightgbm 需要 libgomp1）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir \
       lightgbm \
       google-cloud-bigquery-storage \
       db-dtypes

# 项目源码（按需精简——不带 .claude / data / output 等大目录）
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

# Cloud Run Job 通过 --args 注入 CLI 参数
ENTRYPOINT ["python", "-m", "strategy.ml_multi_horizon_picker.walk_forward"]
