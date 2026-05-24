# Cloud Run Job 容器：跑完整策略回测
# 配套 PRD_20260524_16
FROM python:3.11-slim

WORKDIR /app

# lightgbm 需要 libgomp1；matplotlib 输出中文报告图片时需要基础 CJK 字体。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir \
       lightgbm \
       google-cloud-bigquery-storage \
       db-dtypes

COPY run_backtest.py /app/run_backtest.py
COPY account/ /app/account/
COPY analytics/ /app/analytics/
COPY data_layer/ /app/data_layer/
COPY engine/ /app/engine/
COPY strategy/ /app/strategy/
COPY utils/ /app/utils/
COPY config/backtest.yaml /app/config/backtest.yaml

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV MPLCONFIGDIR=/tmp/matplotlib

ENTRYPOINT ["python", "/app/run_backtest.py"]
