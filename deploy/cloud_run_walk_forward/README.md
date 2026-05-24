# Cloud Run Job 并行走步训练

把 `strategy/ml_multi_horizon_picker/walk_forward.py` 部署到 Cloud Run Jobs，
**8 并发 × 8 时点/任务，~10-15 分钟跑完 5 年完整走步训练**。

配套 PRD：`PRD/PRD_20260524_14_*`。

---

## 快速开始

```bash
# 在项目根目录执行
./deploy/cloud_run_walk_forward/run.sh
```

约 15-20 分钟后，模型会在 `gs://data-aquarium/models/walk_forward/` 下生成。

## 工作流程

```
[run.sh]
  ↓
[1] 确保 Artifact Registry 仓库存在
  ↓
[2] gsutil 上传 config → gs://data-aquarium/configs/walk_forward_cloud_config.yaml
  ↓
[3] gcloud builds submit  → 构建并推送镜像 (Cloud Build, ~3-5 分钟)
  ↓
[4] gcloud run jobs create + execute --wait (~10 分钟)
       ├─ Task 0 → 处理 retrain_dates[0::8]
       ├─ Task 1 → retrain_dates[1::8]
       ├─ ...
       └─ Task 7 → retrain_dates[7::8]
            每个 task 直接把 .pkl + metadata.json 写到 GCS
  ↓
[5] build_registry  → 扫描 GCS 目录，合并出 registry.json
  ↓
✅ 完成
```

## 成本估算

| 项 | 用量 | 价格 | 合计 |
|---|---|---|---|
| Cloud Run Job 执行 | 8 任务 × 4 vCPU × 8 GiB × 0.25h | $0.000024/vCPU-s + $0.0000025/GiB-s | ~HK$0.5 |
| Cloud Build 构建 | ~5 分钟 | 前 120 分钟/天免费 | 0 |
| BigQuery 扫描 | ~5 GB | 免费额度 1 TB/月 | 0 |
| Artifact Registry 存储 | ~500 MB | $0.10/GB/月 | ~HK$0.4/月 |
| GCS 模型存储 | ~100 MB | $0.024/GB/月 | <HK$0.03/月 |
| **首次运行总计** | | | **< HK$1** |

## 可调参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROJECT_ID` | `data-aquarium` | GCP 项目 |
| `REGION` | `asia-east2` | 区域（必须与业务 BQ dataset 同区域）|
| `TASK_COUNT` | `8` | 并发任务数（1 = 单任务串行）|
| `TASK_CPU` | `4` | 单任务 CPU |
| `TASK_MEMORY` | `8Gi` | 单任务内存 |
| `TASK_TIMEOUT` | `3600` | 单任务超时秒数 |
| `SKIP_BUILD` | `0` | =1 跳过镜像构建（复用上次的 IMAGE_TAG）|
| `MODEL_ROOT` | `gs://data-aquarium/models/walk_forward` | 模型存放路径 |

## 小规模测试

先用 2 并发 × limit=4 时点验证管道：

```bash
# 改 walk_forward_cloud_config.yaml 把 final_retrain_date 设为 20200430（5 个月）
TASK_COUNT=2 ./deploy/cloud_run_walk_forward/run.sh
```

预期：5 个时点拆 2 个 task，每个 ~3 个时点，~5 分钟跑完，gs://...models/walk_forward/
下有 5 个 YYYYMMDD 子目录 + 1 个 registry.json。

## 失败排查

### 镜像构建失败
- Cloud Build 日志：`gcloud builds list --region=$REGION --limit=5`
- 看具体某次：`gcloud builds log BUILD_ID --region=$REGION`

### Task 失败
- Cloud Run Job 日志：[Console - Cloud Run Jobs](https://console.cloud.google.com/run/jobs)
- 单个 task 失败不影响其他 task；`build_registry` 会跳过缺失目录
- `--max-retries=1` 已经会自动重试一次

### Service Account 权限不足
默认 Cloud Run Job 使用 Compute Default Service Account
（`PROJECT_NUMBER-compute@developer.gserviceaccount.com`），需要：

```bash
# 给默认 SA 加 BigQuery + Storage 权限
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
SA=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com

for role in roles/bigquery.dataViewer roles/bigquery.jobUser roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA" --role="$role"
done
```

## 跑完后做回测

```bash
# 模型在 GCS，回测先把 registry.json 路径传给策略
python run_backtest.py \
  --strategy strategy.ml_multi_horizon_picker.MLMultiHorizonStrategy \
  --start 20200102 --end 20250430 \
  --capital 1000000 \
  # （需要在策略 preset 里指定 model_registry_path=gs://.../registry.json）
```

## 清理资源（可选）

```bash
# 删除已执行完的 Job 定义
gcloud run jobs delete walk-forward-YYYYMMDD-HHMMSS --region=$REGION

# 删除模型（小心！）
gsutil -m rm -r gs://data-aquarium/models/walk_forward/

# 删除镜像
gcloud artifacts docker images delete $IMAGE --delete-tags
```
