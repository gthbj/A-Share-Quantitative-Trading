# PRD_20260524_14 Cloud Run 并行走步训练

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-24 19:31:00 |
| 文档编号 | PRD_20260524_14 |
| 关联 Commit | 待提交 |
| 需求优先级 | P1 |
| 依赖 | PRD_20260524_13（走步重训）|

## 2. 背景与动机

PRD_20260524_13 提供了走步重训能力，但只能在本地运行：

- 5 年完整走步需要 64 个重训点 × 5 模型 × ~30 秒 ≈ **2-3 小时本地 CPU**
- Mac 笔记本会高负载 + 高温运行半个工作日
- 模型只在本地，每次换设备都要重新训
- 不支持反复试不同超参（每次又是 2-3 小时）

期望行为：把训练**搬到 GCP Cloud Run Job 并行运行**，把总时间压到 ~10-15 分钟，
同时把模型直接落到 GCS 便于共享和重用。

不实现的后果：

- 本地训练 = 单线程串行 = 多人协作/CI 不可用
- 模型在本地 = 不能轻易跨设备 / 跨实验复用

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `strategy/ml_multi_horizon_picker/walk_forward.py` | 改动 | 增加任务分片 + GCS 输出支持 |
| `strategy/ml_multi_horizon_picker/model_registry.py` | 改动 | 支持 gs:// 路径读写 |
| `strategy/ml_multi_horizon_picker/build_registry.py` | 新增 | 扫描 GCS 模型目录合并 registry |
| `deploy/cloud_run_walk_forward/` | 新增 | Dockerfile + 部署脚本 |
| `engine/` / `account/` / `data_layer/` | 不影响 | |
| `tests/` | 新增 | 任务分片单测 + registry GCS 单测 |

## 4. 关键文件路径与现有函数签名

### 4.1 复用既有

```python
# strategy/ml_multi_horizon_picker/walk_forward.py（PRD_13 已交付）
def main(argv: Optional[List[str]] = None) -> int: ...
def list_month_end_dates(start: str, end: str) -> List[str]: ...

# strategy/ml_multi_horizon_picker/model_storage.py（PRD_12 / 既有）
def save_bundle(model_dir: str, ...) -> None: ...   # 已支持 gs:// 路径

# strategy/ml_multi_horizon_picker/model_registry.py（PRD_13）
class ModelRegistry:
    def to_json(self, path: str | Path) -> None: ...   # 当前仅本地
    @classmethod
    def from_json(cls, path) -> "ModelRegistry": ...
```

### 4.2 新增

```
deploy/cloud_run_walk_forward/
├── Dockerfile
├── .dockerignore
├── README.md
└── run.sh            # 一键构建 + push + 启动 + 等待 + 合并 registry

strategy/ml_multi_horizon_picker/
├── build_registry.py # 新增：扫描 GCS 生成 registry.json
└── walk_forward.py   # 改：支持 TASK_INDEX / TASK_COUNT / gs:// 输出
```

## 5. 需求详情

### 5.1 任务分片

`walk_forward.py` 增加从环境变量读分片配置：

- `CLOUD_RUN_TASK_INDEX`（0 ~ TASK_COUNT-1）
- `CLOUD_RUN_TASK_COUNT`（总并发数）

若两者都存在，仅处理 `retrain_dates[TASK_INDEX::TASK_COUNT]`（striding 分片，
让每个任务的负载更均匀，不会出现"前面任务跑长 horizon、后面跑短"的偏差）。

未设置时维持原有"单进程全部跑"行为，本地仍可用。

### 5.2 GCS 输出

允许 `walk_forward.walk_forward.model_root` 是 `gs://bucket/path` 形式：

- 每个时点目录：`gs://data-aquarium/models/walk_forward/{YYYYMMDD}/{buy_h*.pkl, sell_v1.pkl, metadata.json}`
- `model_storage.save_bundle` 已支持，不需要改
- `metadata.json` 改用 `google.cloud.storage` 上传，新增小 helper

### 5.3 注册表合并

每个 Cloud Run 任务只写**自己的模型目录**和**自己的 metadata.json**。
**不写全局 registry.json**（避免并发写覆盖）。

新增独立工具 `strategy.ml_multi_horizon_picker.build_registry` CLI：

```bash
python -m strategy.ml_multi_horizon_picker.build_registry \
    --model-root gs://data-aquarium/models/walk_forward
```

逻辑：
1. 列出 `model_root` 下所有形如 `YYYYMMDD/` 的子目录
2. 验证每个子目录至少有 1 个 `.pkl` 文件（防止部分失败的时点）
3. 生成 `registry.json` 写回 `model_root/registry.json`

这样可以在所有 task 完成后单独调用一次（也支持本地 testing）。

### 5.4 Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app

# 系统依赖（lightgbm 需要 libgomp）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir google-cloud-bigquery-storage

# 项目代码
COPY strategy/ /app/strategy/
COPY utils/ /app/utils/
COPY bigquery_pipeline/ /app/bigquery_pipeline/
COPY account/ /app/account/
COPY engine/ /app/engine/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "strategy.ml_multi_horizon_picker.walk_forward"]
```

启动命令通过 Cloud Run Job 的 `--args` 传入：
`--config /app/walk_forward_config.yaml`

### 5.5 部署脚本 `run.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=data-aquarium
REGION=asia-east2
REPO_NAME=ml-multi-horizon
IMAGE_TAG=$(date +%Y%m%d-%H%M%S)
IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/walk-forward:${IMAGE_TAG}
JOB_NAME=walk-forward-${IMAGE_TAG}
CONFIG_GCS_PATH=gs://data-aquarium/configs/walk_forward_config.yaml
MODEL_ROOT=gs://data-aquarium/models/walk_forward
TASK_COUNT=8

# 0. 一次性：建 Artifact Registry 仓库（已存在则跳过）
gcloud artifacts repositories describe ${REPO_NAME} --location=${REGION} >/dev/null 2>&1 \
  || gcloud artifacts repositories create ${REPO_NAME} \
       --repository-format=docker --location=${REGION}

# 1. 上传配置到 GCS（避免镜像里硬编码）
gsutil cp deploy/cloud_run_walk_forward/walk_forward_cloud_config.yaml ${CONFIG_GCS_PATH}

# 2. 构建并推送镜像
gcloud builds submit --tag ${IMAGE} \
  --region=${REGION} \
  -f deploy/cloud_run_walk_forward/Dockerfile .

# 3. 创建 Cloud Run Job
gcloud run jobs create ${JOB_NAME} \
  --image ${IMAGE} \
  --region ${REGION} \
  --tasks ${TASK_COUNT} \
  --parallelism ${TASK_COUNT} \
  --task-timeout 3600 \
  --memory 8Gi --cpu 4 \
  --max-retries 1 \
  --set-env-vars CLOUD_RUN_TASK_COUNT=${TASK_COUNT} \
  --set-env-vars CONFIG_GCS=${CONFIG_GCS_PATH} \
  --args "--config-gcs=${CONFIG_GCS_PATH}"

# 4. 执行并等待
gcloud run jobs execute ${JOB_NAME} --region ${REGION} --wait

# 5. 合并 registry
python -m strategy.ml_multi_horizon_picker.build_registry \
  --model-root ${MODEL_ROOT}

# 6. 清理 job 定义（可选）
gcloud run jobs delete ${JOB_NAME} --region ${REGION} --quiet
```

Cloud Run Job 会自动给每个 task 注入 `CLOUD_RUN_TASK_INDEX` 环境变量。
我们在 walk_forward 里读它做 striding 分片。

### 5.6 配置变更

新增 `deploy/cloud_run_walk_forward/walk_forward_cloud_config.yaml`，
与现有 `walk_forward_config.yaml` 完全一致，**仅 model_root 改为 gs:// 路径**：

```yaml
walk_forward:
  model_root: "gs://data-aquarium/models/walk_forward"
  # ... 其他同 walk_forward_config.yaml
```

## 6. 不可改动的红线

- **不改动 PRD_12 / PRD_13 已交付的核心训练逻辑**（labels.py / features.py / regime.py /
  model_storage.py / strategy.py）——本 PRD 只做"部署形态"扩展
- **不破坏本地训练能力**——未设置 TASK_INDEX/COUNT 时行为不变
- **不动 main 业务表 dataset / 不动其他 agent 已合入的工作**

## 7. 修改范围与位置

### 7.1 新增

| 文件 | 行数 | 内容 |
|---|---|---|
| `deploy/cloud_run_walk_forward/Dockerfile` | ~20 | 容器镜像 |
| `deploy/cloud_run_walk_forward/.dockerignore` | ~10 | 排除大目录 |
| `deploy/cloud_run_walk_forward/walk_forward_cloud_config.yaml` | ~50 | 云端配置 |
| `deploy/cloud_run_walk_forward/run.sh` | ~50 | 一键部署脚本 |
| `deploy/cloud_run_walk_forward/README.md` | ~80 | 使用文档 |
| `strategy/ml_multi_horizon_picker/build_registry.py` | ~80 | 扫描 GCS 生成 registry |
| `tests/test_strategy_ml_multi_horizon_cloud_run.py` | ~150 | 分片 + GCS registry 单测 |

### 7.2 改动

| 文件 | 改动 |
|---|---|
| `strategy/ml_multi_horizon_picker/walk_forward.py` | 增加 `--config-gcs` 选项；读 `CLOUD_RUN_TASK_INDEX/COUNT` 做分片；支持 gs:// model_root（model storage 已支持，本文件主要是把 metadata.json 也走 GCS）|
| `strategy/ml_multi_horizon_picker/model_registry.py` | `to_json/from_json` 支持 gs:// 路径（用 google.cloud.storage）|

### 7.3 不修改

- `strategy/ml_multi_horizon_picker/labels.py / features.py / regime.py / model_storage.py /
  strategy.py / train.py / config.yaml`
- `engine/` / `account/` / `data_layer/` / `analytics/`
- `bigquery_pipeline/` / `gcs_to_bigquery/` / `data_transfer/`

## 8. 验收标准

### 8.1 单元测试

1. 设置 `CLOUD_RUN_TASK_INDEX=0 CLOUD_RUN_TASK_COUNT=4` 时，`walk_forward` 只处理 stride 索引 0/4/8/...
2. `ModelRegistry.to_json/from_json` 接受 `gs://` 路径并能成功 roundtrip（mock GCS）
3. `build_registry` 在给定 mock 目录下生成正确的 `registry.json`

### 8.2 端到端

4. 小规模 POC 跑通：`TASK_COUNT=2, retrain_dates=[2019-12-31, 2020-01-31, 2020-02-29]`，
   两个 task 各处理 ~1.5 个时点，产物落到 `gs://data-aquarium/models/walk_forward_test/`
5. 全量跑：8 并发 × 8 时点/任务 = 64 时点，总耗时 < 30 分钟，模型全部落 GCS

### 8.3 不破坏

6. 本地不设置 TASK_INDEX/COUNT 时，`walk_forward` 行为与 PRD_13 完全一致
7. PRD_12 / PRD_13 既有 37 个单测继续全过

### 8.4 测试用例（项目偏好 §9）

**用例 1：任务分片正确**
```
输入：retrain_dates=[20191231, 20200131, 20200229, 20200331, 20200430, 20200531, 20200630, 20200731]
TASK_INDEX=0 TASK_COUNT=4
预期：本任务处理 [20191231, 20200430]（stride 0, 4）
TASK_INDEX=1 处理 [20200131, 20200531]
TASK_INDEX=2 处理 [20200229, 20200630]
TASK_INDEX=3 处理 [20200331, 20200731]
```

**用例 2：未设置环境变量时全部跑**
```
输入：无 TASK_INDEX / TASK_COUNT
预期：处理全部 retrain_dates
```

**用例 3：GCS registry 合并**
```
输入：GCS 路径下有 {20191231, 20200131, 20200229} 三个目录，每个含 buy_h5.pkl
调用：build_registry --model-root gs://...
预期：生成 registry.json 三条记录，train_end_date 升序
```

## 9. 备注

### 9.1 成本估算

| 项 | 用量 | 单价 | 合计 |
|---|---|---|---|
| Cloud Run Job 执行 | 4 vCPU × 8 Gi × 0.5h × 8 并发 | $0.000024/vCPU-s + $0.000003/GiB-s | ~HK$0.5 |
| BigQuery 扫描 | ~5 GB | 在免费额度内 | 0 |
| Artifact Registry 存储 | ~500 MB | $0.10/GB/月 | ~HK$0.4/月 |
| GCS 模型存储 | ~100 MB | $0.024/GB/月 | <HK$0.03/月 |
| Cloud Build 构建 | ~5 分钟 | 前 120 分钟/天免费 | 0 |
| **首次构建 + 运行** | | | **< HK$1** |
| **稳态月费**（不重新训）| | | < HK$0.5 |

### 9.2 失败回退

如果 Cloud Run Job 失败：
- 单个 task 失败不会污染其他 task 的输出（每个 task 写不同目录）
- `--max-retries 1` 自动重试一次
- 重试后仍失败：`build_registry` 会跳过缺失目录，回测仍可用其他 64-N 个时点

如果整体跑挂了（罕见）：
- 直接重新执行 `run.sh`；已成功的时点会被新 task 覆盖（同名目录），但模型内容确定性所以等价

### 9.3 安全

- Cloud Run Job 使用 Compute Default Service Account
- 需要权限：BigQuery Data Viewer + Job User + Storage Object Admin（写 GCS）
- 不在镜像里嵌入凭据，全部走 ADC

### 9.4 与既有 PRD 关系

- 严格延续 PRD_13（走步重训）的训练逻辑
- 不与其他 agent 的 PRD_07-11 工作冲突（Cloud Run Job 是独立部署，没碰他们的代码）
- 与 PRD_04（账单导出）不相关
