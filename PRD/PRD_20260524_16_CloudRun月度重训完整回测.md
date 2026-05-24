# PRD_20260524_16 CloudRun月度重训完整回测

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-24 22:36:52 |
| 文档编号 | PRD_20260524_16 |
| 关联 Commit | 待提交 |
| 需求优先级 | P0 |

## 2. 背景与动机

用户要求按“月度重训”的真实交易模拟方案跑完整回测。当前 GCS 模型注册表
`gs://data-aquarium/models/walk_forward/registry.json` 最后一个模型时点是 `20250331`，
而目标回测区间是 `20200102 ~ 20260430`。如果直接回测，`2025-04-01` 到 `2026-04-30`
会一直使用 `20250331` 模型，不能代表月度重训方案。

同时，本地 Mac 只有 8GB 内存，不适合跑完整 6 年、500 只股票池、月度模型切换的回测。
应在 GCP Cloud Run Job 中完成：

1. 追加训练 `20250430 ~ 20260331` 的月度模型。
2. 合并 GCS registry。
3. 用 Cloud Run Job 跑 `20200102 ~ 20260430` 的完整回测。
4. 输出 HTML / Markdown / NAV / trades，并归档到 GCS。

不实现的后果：

- 回测尾段模型过旧，无法回答“月度重训方案”的真实表现。
- 本地完整回测耗时长且可能受内存影响。
- 回测产物不在 GCS，后续对比、复查和归档不稳定。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `deploy/cloud_run_backtest/` | 新增 | Cloud Run 回测镜像、构建和执行脚本 |
| `deploy/cloud_run_walk_forward/` | 新增配置 | 追加训练到 2026-03-31 的云端配置 |
| `.dockerignore` / `.gcloudignore` | 新增 | 防止上传本地数据、输出、密钥和 `.claude` 到 Cloud Build |
| `ARCHITECTURE.md` | 影响 | 补充 Cloud Run 回测部署说明 |
| `strategy` / `engine` / `data_layer` | 不新增逻辑 | 复用 PRD_15 已完成的 GCS 模型读取、warmup 和动态 universe |

**回测可复现性**：同一份 GCS registry、BigQuery 数据、配置、镜像 tag 和参数下，回测结果应可复现。

## 4. 关键文件路径与现有函数签名

```python
# run_backtest.py
def main() -> int:
    parser = argparse.ArgumentParser(description="A股模拟量化交易回测")
    parser.add_argument("--preset", default=None, ...)
    parser.add_argument("--start", default=None, ...)
    parser.add_argument("--end", default=None, ...)
    parser.add_argument("--capital", type=float, default=None, ...)
    parser.add_argument("--gcs-archive-uri", default=None, ...)
    ...
```

```bash
# deploy/cloud_run_walk_forward/run.sh
gcloud run jobs create "$JOB_NAME" \
    --image="$IMAGE" \
    --tasks="$TASK_COUNT" \
    --parallelism="$TASK_COUNT" \
    --args="--config-gcs=$CONFIG_GCS_PATH,--skip-registry"
```

```python
# strategy/ml_multi_horizon_picker/build_registry.py
def main(argv: Optional[List[str]] = None) -> int:
    parser.add_argument("--model-root", required=True)
    ...
```

## 5. 需求详情

### 5.1 追加月度重训

新增配置 `deploy/cloud_run_walk_forward/walk_forward_append_202604_config.yaml`：

- `initial_train_end: "20250430"`
- `final_retrain_date: "20260331"`
- `model_root: "gs://data-aquarium/models/walk_forward"`
- 其余训练参数与当前 Cloud Run walk-forward 配置保持一致。

执行时使用现有 walk-forward 镜像即可，不需要重新训练 2019-2025 已存在模型。训练完成后运行：

```bash
python -m strategy.ml_multi_horizon_picker.build_registry \
  --model-root gs://data-aquarium/models/walk_forward
```

### 5.2 Cloud Run 完整回测 Job

新增 `deploy/cloud_run_backtest/`：

```text
deploy/cloud_run_backtest/
├── Dockerfile
├── cloudbuild.yaml
└── run.sh
```

Dockerfile 只复制运行回测必要文件：

- `run_backtest.py`
- `account/`
- `analytics/`
- `data_layer/`
- `engine/`
- `strategy/`
- `utils/`
- `config/backtest.yaml`

不得复制 `config/secrets.yaml`、`data/`、`output/`、`.claude/`。

### 5.3 回测参数

Cloud Run Job 运行：

```bash
python /app/run_backtest.py \
  --preset ml_multi_horizon_picker \
  --start 20200102 \
  --end 20260430 \
  --capital 100000 \
  --output /tmp/backtest_runs \
  --run-name monthly_retrain_20200102_20260430 \
  --gcs-archive-uri gs://data-aquarium/a-share/backtest_runs/ml_multi_horizon_picker_monthly
```

说明：

- `20200101` 是休市日，实际起点使用 `20200102`。
- preset 内 `model_registry_path` 指向 GCS registry。
- 模型切换必须按 `train_end_date < current_date` 取最新模型；月末当天收盘后训练出的模型只能从下一个交易日开始使用，避免月末当天回测信号包含当天收盘后才产生的训练结果。
- 回测产物归档到 GCS，不依赖本地文件系统保留。

## 6. 配置变更

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `WF_APPEND_CONFIG_GCS_PATH` | env | `gs://data-aquarium/configs/walk_forward_append_202604_config.yaml` | 追加训练配置 |
| `BACKTEST_GCS_ARCHIVE_URI` | env | `gs://data-aquarium/a-share/backtest_runs/ml_multi_horizon_picker_monthly` | 回测产物归档位置 |
| `BACKTEST_START` | env | `20200102` | 回测起点 |
| `BACKTEST_END` | env | `20260430` | 回测终点 |
| `BACKTEST_CAPITAL` | env | `100000` | 初始资金 |
| `TASK_TIMEOUT` | env | `28800` | Cloud Run 单任务超时，完整逐日评分回测默认 8 小时 |

## 7. 不可改动的红线区域

- 不覆盖已存在的 `20191231 ~ 20250331` 模型目录。
- 不删除 GCS 模型、回测产物或 BigQuery 表。
- 不复制 `config/secrets.yaml` 到镜像或 Cloud Build 上传包。
- 不改变撮合规则、滑点、费用、T+1 规则。
- 不打开账户专项交易权限，仍保持 `false`。
- 不使用未来数据：每日决策只能使用该交易日前已训练完成的模型；月末模型从下一个交易日生效。`20260430` 决策最多只能使用该日前已存在的模型和当日收盘后数据生成次日信号；本次回测记录到 `20260430`。

## 8. 修改范围与位置

### 8.1 主要修改文件

| 文件 | 修改内容 |
|---|---|
| `deploy/cloud_run_walk_forward/walk_forward_append_202604_config.yaml` | 追加训练配置 |
| `deploy/cloud_run_backtest/Dockerfile` | 回测镜像 |
| `deploy/cloud_run_backtest/cloudbuild.yaml` | Cloud Build 构建回测镜像 |
| `deploy/cloud_run_backtest/run.sh` | 创建/执行 Cloud Run 回测 Job |
| `.dockerignore` / `.gcloudignore` | 排除本地数据、输出和密钥 |
| `ARCHITECTURE.md` | 更新 Cloud Run 回测说明 |

### 8.2 不修改文件

- `engine/trade_engine.py`
- `account/portfolio.py`
- `strategy/ml_multi_horizon_picker/walk_forward.py`
- `bigquery_pipeline/`
- `gcs_to_bigquery/`

## 9. 验收标准

1. 追加模型：
   - 输入：`20250430 ~ 20260331` 追加训练配置。
   - 预期：GCS 模型目录新增对应月末目录，每个目录包含 `buy_h1.pkl`、`buy_h5.pkl`、`buy_h10.pkl`、`buy_h20.pkl` 和 `sell_v1.pkl`。

2. registry：
   - 输入：扫描 `gs://data-aquarium/models/walk_forward`。
   - 预期：`registry.json` 最后一个 `train_end_date` 为 `20260331`。

3. 完整回测：
   - 输入：`ml_multi_horizon_picker`，`20200102 ~ 20260430`，初始资金 `100000`。
   - 预期：Cloud Run Job 成功退出，GCS 归档目录包含 `summary.md`、`report.html`、`nav.csv`、`trades.csv`、`gcs_archive_manifest.json`。

4. 结果汇报：
   - 输出累计收益率、年化收益率、最大回撤、夏普、交易次数、GCS 产物路径和本次执行耗时。

## 10. 备注

本 PRD 只负责把月度重训方案完整跑通。模型是否应引入 PE/PB/ROE、龙虎榜、资金流、开盘啦事件等增强特征，应另写 PRD 设计 v2 模型。
