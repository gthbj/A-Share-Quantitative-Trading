#!/usr/bin/env bash
# Cloud Run Job 走步训练一键部署脚本（PRD_20260524_14）
#
# 用法（在项目根目录执行）：
#   ./deploy/cloud_run_walk_forward/run.sh                     # 全量 8 并发
#   TASK_COUNT=2 ./deploy/cloud_run_walk_forward/run.sh        # 2 并发（小规模测试）
#   SKIP_BUILD=1 ./deploy/cloud_run_walk_forward/run.sh        # 跳过镜像构建，复用已有 IMAGE_TAG
#
# 前置条件：
#   - gcloud CLI 已登录，且账号有 Artifact Registry / Cloud Build / Cloud Run / BigQuery 权限
#   - GCS bucket gs://data-aquarium 存在
#   - 当前 cwd 是项目根目录

set -euo pipefail

# ── 参数 ───────────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-data-aquarium}"
REGION="${REGION:-asia-east2}"
REPO_NAME="${REPO_NAME:-ml-multi-horizon}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/walk-forward:${IMAGE_TAG}"
JOB_NAME="${JOB_NAME:-walk-forward-${IMAGE_TAG}}"
CONFIG_GCS_PATH="${CONFIG_GCS_PATH:-gs://data-aquarium/configs/walk_forward_cloud_config.yaml}"
MODEL_ROOT="${MODEL_ROOT:-gs://data-aquarium/models/walk_forward}"
TASK_COUNT="${TASK_COUNT:-8}"
TASK_MEMORY="${TASK_MEMORY:-8Gi}"
TASK_CPU="${TASK_CPU:-4}"
TASK_TIMEOUT="${TASK_TIMEOUT:-3600}"

LOCAL_CONFIG="${LOCAL_CONFIG:-deploy/cloud_run_walk_forward/walk_forward_cloud_config.yaml}"

echo "━━━━━━━━━━ 配置 ━━━━━━━━━━"
echo "  PROJECT_ID  = $PROJECT_ID"
echo "  REGION      = $REGION"
echo "  IMAGE       = $IMAGE"
echo "  JOB_NAME    = $JOB_NAME"
echo "  CONFIG      = $CONFIG_GCS_PATH"
echo "  MODEL_ROOT  = $MODEL_ROOT"
echo "  TASK_COUNT  = $TASK_COUNT  (并行任务数)"
echo "  TASK_RES    = ${TASK_CPU} vCPU × ${TASK_MEMORY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 0. 一次性：Artifact Registry 仓库 ──────────────────────────────
echo "[0/5] 确保 Artifact Registry 仓库存在..."
if ! gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "       仓库不存在，创建中..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --description="ML Multi-Horizon 走步训练镜像"
fi

# ── 1. 上传配置到 GCS ──────────────────────────────────────────────
echo "[1/5] 上传配置 $LOCAL_CONFIG → $CONFIG_GCS_PATH"
gsutil -q cp "$LOCAL_CONFIG" "$CONFIG_GCS_PATH"

# ── 2. 构建镜像（Cloud Build）─────────────────────────────────────
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "[2/5] 跳过镜像构建（SKIP_BUILD=1）；使用 IMAGE_TAG=$IMAGE_TAG"
else
    echo "[2/5] 提交 Cloud Build 构建镜像（约 3-5 分钟）..."
    gcloud builds submit \
        --tag "$IMAGE" \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --config=/dev/stdin <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '$IMAGE', '-f', 'deploy/cloud_run_walk_forward/Dockerfile', '.']
images: ['$IMAGE']
EOF
fi

# ── 3. 创建 / 更新 Cloud Run Job ────────────────────────────────
echo "[3/5] 创建 Cloud Run Job: $JOB_NAME"
gcloud run jobs create "$JOB_NAME" \
    --image="$IMAGE" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --tasks="$TASK_COUNT" \
    --parallelism="$TASK_COUNT" \
    --task-timeout="$TASK_TIMEOUT" \
    --memory="$TASK_MEMORY" \
    --cpu="$TASK_CPU" \
    --max-retries=1 \
    --set-env-vars="CLOUD_RUN_TASK_COUNT=$TASK_COUNT" \
    --args="--config-gcs=$CONFIG_GCS_PATH,--skip-registry"

# ── 4. 执行并等待 ─────────────────────────────────────────────────
echo "[4/5] 执行 Cloud Run Job（--wait 阻塞到完成）..."
gcloud run jobs execute "$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --wait

# ── 5. 合并 registry ──────────────────────────────────────────────
echo "[5/5] 扫描 GCS 模型目录，合并 registry.json"
python -m strategy.ml_multi_horizon_picker.build_registry \
    --model-root "$MODEL_ROOT"

echo "✅ 完成！"
echo "   模型目录: $MODEL_ROOT"
echo "   注册表  : $MODEL_ROOT/registry.json"
echo ""
echo "回测命令示例："
echo "  python run_backtest.py \\"
echo "    --strategy strategy.ml_multi_horizon_picker.MLMultiHorizonStrategy \\"
echo "    --start 20200102 --end 20250430 --capital 1000000"
echo ""
echo "Job 定义保留在 Cloud Run（如需清理）："
echo "  gcloud run jobs delete $JOB_NAME --region $REGION --quiet"
