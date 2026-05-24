#!/usr/bin/env bash
# Cloud Run Job 富特征走步训练一键部署脚本（PRD_20260525_03）
#
# 用法（项目根目录执行）::
#   ./deploy/cloud_run_walk_forward_rich/run.sh
#
# 复用了 walk_forward_rich/run.sh 的所有套路，只换：
#   - Dockerfile 路径 → cloud_run_walk_forward_rich/Dockerfile
#   - cloudbuild.yaml 路径 → cloud_run_walk_forward_rich/cloudbuild.yaml
#   - 配置文件 → walk_forward_rich_cloud_config.yaml
#   - GCS 路径 → gs://data-aquarium/configs/walk_forward_rich_cloud_config.yaml
#   - 模型路径 → gs://data-aquarium/models/walk_forward_rich
#   - Artifact Registry repo → ml-rich-picker

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-data-aquarium}"
REGION="${REGION:-asia-east2}"
REPO_NAME="${REPO_NAME:-ml-rich-picker}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/walk-forward-rich:${IMAGE_TAG}"
JOB_NAME="${JOB_NAME:-walk-forward-rich-${IMAGE_TAG}}"
CONFIG_GCS_PATH="${CONFIG_GCS_PATH:-gs://data-aquarium/configs/walk_forward_rich_cloud_config.yaml}"
MODEL_ROOT="${MODEL_ROOT:-gs://data-aquarium/models/walk_forward_rich}"
TASK_COUNT="${TASK_COUNT:-5}"
TASK_MEMORY="${TASK_MEMORY:-8Gi}"
TASK_CPU="${TASK_CPU:-4}"
TASK_TIMEOUT="${TASK_TIMEOUT:-3600}"

LOCAL_CONFIG="${LOCAL_CONFIG:-deploy/cloud_run_walk_forward_rich/walk_forward_rich_cloud_config.yaml}"

echo "━━━━━━━━━━ Rich 走步训练配置 ━━━━━━━━━━"
echo "  PROJECT_ID  = $PROJECT_ID"
echo "  REGION      = $REGION"
echo "  IMAGE       = $IMAGE"
echo "  JOB_NAME    = $JOB_NAME"
echo "  CONFIG      = $CONFIG_GCS_PATH"
echo "  MODEL_ROOT  = $MODEL_ROOT"
echo "  TASK_COUNT  = $TASK_COUNT  (并行任务数)"
echo "  TASK_RES    = ${TASK_CPU} vCPU × ${TASK_MEMORY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 0. Artifact Registry 仓库
echo "[0/5] 确保 Artifact Registry 仓库存在..."
if ! gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "       仓库不存在，创建中..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --description="ML Rich Picker 富特征走步训练镜像"
fi

# 1. 上传配置到 GCS
echo "[1/5] 上传配置 $LOCAL_CONFIG → $CONFIG_GCS_PATH"
gsutil -q cp "$LOCAL_CONFIG" "$CONFIG_GCS_PATH"

# 2. 构建镜像
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "[2/5] 跳过镜像构建（SKIP_BUILD=1）；使用 IMAGE_TAG=$IMAGE_TAG"
else
    echo "[2/5] 提交 Cloud Build 构建镜像（约 3-5 分钟）..."
    gcloud builds submit \
        --config=deploy/cloud_run_walk_forward_rich/cloudbuild.yaml \
        --substitutions=_IMAGE="$IMAGE" \
        --region="$REGION" \
        --project="$PROJECT_ID"
fi

# 3. 创建 Cloud Run Job
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
    --args="--config-gcs=$CONFIG_GCS_PATH,--skip-registry"

# 4. 执行并等待
echo "[4/5] 执行 Cloud Run Job（--wait 阻塞到完成）..."
gcloud run jobs execute "$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --wait

# 5. 合并 registry
echo "[5/5] 扫描 GCS 模型目录，合并 registry.json"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"
if [ -z "$PYTHON_BIN" ]; then
    echo "⚠️ 找不到 python/python3。请手动执行："
    echo "  python -m strategy.ml_multi_horizon_picker.build_registry --model-root $MODEL_ROOT"
    exit 1
fi
"$PYTHON_BIN" -m strategy.ml_multi_horizon_picker.build_registry \
    --model-root "$MODEL_ROOT"

echo "✅ Rich 走步训练完成！"
echo "   模型目录: $MODEL_ROOT"
echo "   注册表  : $MODEL_ROOT/registry.json"
echo ""
echo "回测命令示例（需更新 preset 中 model_registry_path）："
echo "  python run_backtest.py --preset ml_rich_picker --start 20200102 --end 20250430"
