#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-data-aquarium}"
REGION="${REGION:-asia-east2}"
REPO_NAME="${REPO_NAME:-tushare-ingest}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/tushare-ingest:${IMAGE_TAG}"
JOB_NAME="${JOB_NAME:-tushare-ingest-${IMAGE_TAG}}"
PRIORITY="${PRIORITY:-p0}"
PRIORITY_THROUGH="${PRIORITY_THROUGH:-}"
START_DATE="${START_DATE:-20190101}"
END_DATE="${END_DATE:-$(date +%Y%m%d)}"
TASK_MEMORY="${TASK_MEMORY:-2Gi}"
TASK_CPU="${TASK_CPU:-1}"
TASK_TIMEOUT="${TASK_TIMEOUT:-14400}"
TUSHARE_SECRET="${TUSHARE_SECRET:-tushare-token:latest}"
TUSHARE_HTTP_URL="${TUSHARE_HTTP_URL:-http://118.89.66.41:8010/}"

echo "Tushare -> GCS ingestion"
echo "  PROJECT_ID = $PROJECT_ID"
echo "  REGION     = $REGION"
echo "  IMAGE      = $IMAGE"
echo "  JOB_NAME   = $JOB_NAME"
echo "  PRIORITY   = ${PRIORITY:-<unset>}"
echo "  THROUGH    = ${PRIORITY_THROUGH:-<unset>}"
echo "  RANGE      = $START_DATE -> $END_DATE"
echo "  HTTP_URL   = $TUSHARE_HTTP_URL"

if [ -n "$PRIORITY" ] && [ -n "$PRIORITY_THROUGH" ]; then
    echo "PRIORITY and PRIORITY_THROUGH are mutually exclusive." >&2
    exit 2
fi

if [ -n "$PRIORITY_THROUGH" ]; then
    PRIORITY_ARGS=(--priority-through "$PRIORITY_THROUGH")
else
    PRIORITY_ARGS=(--priority "$PRIORITY")
fi

if ! gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --description="Tushare ingestion images"
fi

gcloud builds submit \
    --config=deploy/cloud_run_tushare_ingest/cloudbuild.yaml \
    --substitutions=_IMAGE="$IMAGE" \
    --region="$REGION" \
    --project="$PROJECT_ID"

gcloud run jobs create "$JOB_NAME" \
    --image="$IMAGE" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --tasks=1 \
    --parallelism=1 \
    --task-timeout="$TASK_TIMEOUT" \
    --memory="$TASK_MEMORY" \
    --cpu="$TASK_CPU" \
    --max-retries=1 \
    --set-env-vars="TUSHARE_HTTP_URL=${TUSHARE_HTTP_URL}" \
    --set-secrets="TUSHARE_TOKEN=${TUSHARE_SECRET}" \
    --args="${PRIORITY_ARGS[0]},${PRIORITY_ARGS[1]},--start-date,${START_DATE},--end-date,${END_DATE}"

gcloud run jobs execute "$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --wait
