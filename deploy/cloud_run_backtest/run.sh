#!/usr/bin/env bash
# Cloud Run Job 完整回测一键部署脚本（PRD_20260524_16）
#
# 用法（在项目根目录执行）：
#   ./deploy/cloud_run_backtest/run.sh
#   SKIP_BUILD=1 IMAGE_TAG=20260524-230000 ./deploy/cloud_run_backtest/run.sh
#
# 前置条件：
#   - gcloud CLI 已登录，且账号有 Artifact Registry / Cloud Build / Cloud Run / BigQuery 权限
#   - GCS bucket gs://data-aquarium 存在
#   - 当前 cwd 是项目根目录

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-data-aquarium}"
REGION="${REGION:-asia-east2}"
REPO_NAME="${REPO_NAME:-ml-multi-horizon}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/backtest:${IMAGE_TAG}"
JOB_NAME="${JOB_NAME:-ml-backtest-${IMAGE_TAG}}"

BACKTEST_START="${BACKTEST_START:-20200102}"
BACKTEST_END="${BACKTEST_END:-20260430}"
BACKTEST_CAPITAL="${BACKTEST_CAPITAL:-100000}"
BACKTEST_OUTPUT="${BACKTEST_OUTPUT:-/tmp/backtest_runs}"
BACKTEST_RUN_NAME="${BACKTEST_RUN_NAME:-monthly_retrain_${BACKTEST_START}_${BACKTEST_END}}"
BACKTEST_GCS_ARCHIVE_URI="${BACKTEST_GCS_ARCHIVE_URI:-gs://data-aquarium/a-share/backtest_runs/ml_multi_horizon_picker_monthly}"

TASK_MEMORY="${TASK_MEMORY:-8Gi}"
TASK_CPU="${TASK_CPU:-4}"
TASK_TIMEOUT="${TASK_TIMEOUT:-28800}"

echo "━━━━━━━━━━ 配置 ━━━━━━━━━━"
echo "  PROJECT_ID   = $PROJECT_ID"
echo "  REGION       = $REGION"
echo "  IMAGE        = $IMAGE"
echo "  JOB_NAME     = $JOB_NAME"
echo "  BACKTEST     = $BACKTEST_START ~ $BACKTEST_END"
echo "  CAPITAL      = $BACKTEST_CAPITAL"
echo "  GCS_ARCHIVE  = $BACKTEST_GCS_ARCHIVE_URI"
echo "  TASK_RES     = ${TASK_CPU} vCPU × ${TASK_MEMORY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "[0/4] 确保 Artifact Registry 仓库存在..."
if ! gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "       仓库不存在，创建中..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --description="ML Multi-Horizon training and backtest images"
fi

if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "[1/4] 跳过镜像构建（SKIP_BUILD=1）；使用 IMAGE_TAG=$IMAGE_TAG"
else
    echo "[1/4] 提交 Cloud Build 构建回测镜像（约 3-5 分钟）..."
    gcloud builds submit \
        --config=deploy/cloud_run_backtest/cloudbuild.yaml \
        --substitutions=_IMAGE="$IMAGE" \
        --region="$REGION" \
        --project="$PROJECT_ID"
fi

echo "[2/4] 创建 Cloud Run Job: $JOB_NAME"
gcloud run jobs create "$JOB_NAME" \
    --image="$IMAGE" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --tasks=1 \
    --parallelism=1 \
    --task-timeout="$TASK_TIMEOUT" \
    --memory="$TASK_MEMORY" \
    --cpu="$TASK_CPU" \
    --max-retries=0 \
    --args="--preset,ml_multi_horizon_picker,--start,$BACKTEST_START,--end,$BACKTEST_END,--capital,$BACKTEST_CAPITAL,--output,$BACKTEST_OUTPUT,--run-name,$BACKTEST_RUN_NAME,--gcs-archive-uri,$BACKTEST_GCS_ARCHIVE_URI,--daily-diagnostics,--daily-candidate-top-n,10"

echo "[3/4] 执行 Cloud Run Job（--wait 阻塞到完成）..."
gcloud run jobs execute "$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --wait

echo "[4/4] 完成。"
echo "查看日志："
echo "  gcloud run jobs executions describe --job=$JOB_NAME --region=$REGION --project=$PROJECT_ID"
echo "GCS 归档前缀："
echo "  $BACKTEST_GCS_ARCHIVE_URI"
