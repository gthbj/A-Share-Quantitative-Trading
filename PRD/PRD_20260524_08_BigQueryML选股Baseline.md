# BigQuery ML 选股 Baseline PRD

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| LLM 型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-24 13:23:22 |
| 文档编号 | PRD_20260524_08 |
| 关联 Commit | 待提交 |
| 需求优先级 | P1 |

## 2. 背景与动机

本地 Mac 为 8GB 内存，不适合直接用 pandas 拉取 `ashare.dws_*` 全量特征训练 LightGBM/XGBoost。当前数据已经在 BigQuery 内完成 ODS/DWD/DWS/ADS 分层，ML 选股训练所需的技术、估值、基本面、事件/资金流特征都已在 BigQuery DWS 层可用。因此需要先做一版 BigQuery ML baseline：

- 不把全量数据拉回本地。
- 直接在 BigQuery 内训练 boosted tree classifier。
- 在 BigQuery 内生成每日截面预测排名和 Top-N 候选信号。
- 作为本地 LightGBM 训练前的可用基线和特征口径验证。

BigQuery ML baseline 不替代后续 LightGBM/XGBoost 模型，只用于快速建立可比较、低运维的基线。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
| --- | --- | --- |
| `bigquery_pipeline` | 是 | 新增 BigQuery ML 训练、预测和审计命令 |
| `config` | 是 | 新增 BQML baseline 默认参数 |
| `tests` | 是 | 增加 SQL 构建和 CLI 暴露测试 |
| `ARCHITECTURE.md` | 是 | 同步 BigQuery ML baseline 说明 |
| `strategy` | 否 | 本需求不改 Python 策略下单逻辑 |
| `data_layer` / `engine` / `account` / `analytics` | 否 | 不修改 |

是否影响回测结果可复现性：不直接影响。本需求只在 BigQuery 生成模型和预测表，不改变 `run_backtest.py` 默认行为。

## 4. 关键文件路径与现有函数签名

```python
# bigquery_pipeline/cli.py
def main() -> int:
    parser = argparse.ArgumentParser(description="Run BigQuery-internal A-share DWD/DWS/ADS pipeline tasks.")
```

```python
# bigquery_pipeline/client.py
def bq_client(config: dict):
    bigquery = require_bigquery()
    kwargs = {"project": config["project_id"], "location": config.get("location")}
```

```python
# bigquery_pipeline/ads.py
def build_ml_stock_picker_signal_sql(config: dict) -> str:
    top_n = int(ads_config(config).get("ml_stock_picker_top_n", 50))
```

```yaml
# bigquery_pipeline/config.yaml
defaults:
  ads:
    ml_stock_picker_top_n: 50
```

## 5. 需求详情

### 5.1 功能目标

新增 BigQuery ML baseline：

1. 训练模型：
   - 模型名：`ashare.bqml_ml_stock_picker_baseline`
   - 模型类型：`BOOSTED_TREE_CLASSIFIER`
   - 标签：未来 `label_horizon` 日收益在当日截面 top 30% 为 1，bottom 30% 为 0，中间样本丢弃。
   - 特征：DWS 技术指标 + DWS 基本面估值 + 事件/资金流。
   - 使用 `is_eval` 做 custom split，训练和评估按日期切分，避免未来泄漏。

2. 生成预测信号表：
   - 表名：`ashare.ads_signal_ml_stock_picker_bqml_1d`
   - 对预测区间内每日股票截面计算 `prob_up`。
   - 每日按 `prob_up DESC` 排名，`score_rank <= top_n` 标记为候选。

3. 审计：
   - 模型存在。
   - `ML.EVALUATE` 能返回 `roc_auc` 等评估指标。
   - 预测表存在且非空。
   - 每日候选数不超过 `top_n`。

### 5.2 数据口径

训练特征来源：

- `ashare.dws_equity_daily_features`
- `ashare.dws_equity_fundamental_features`
- `ashare.dws_equity_event_money_flow_features_1d`

特征字段：

- 技术：`return_1d`、`return_5d`、`return_10d`、`return_20d`、`volume_ma20_ratio`、`amount_ma5_ratio`、`std_ratio`、`rsi_14`、`macd_hist`、`close_to_ma20`
- 基本面/估值：`pe_basic`、`pb`、`roe`、`gross_margin`、`net_margin`、`debt_to_assets`、`current_ratio`、`asset_turnover`、`market_cap_log`
- 事件/资金流：`net_inflow_to_amount`、`main_net_inflow_to_amount`、`dragon_tiger_net_to_amount`、`dragon_tiger_department_count`、`limit_up_streak`、`is_kpl_event`

所有训练和预测 SQL 不得使用未来收益作为预测特征；`label_return` 只允许在训练 CTE 中生成标签。

### 5.3 交互流程

```text
python -m bigquery_pipeline.cli train-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
  -> CREATE OR REPLACE MODEL ashare.bqml_ml_stock_picker_baseline
  -> BigQuery 内部训练，不拉取全量数据到本地
```

```text
python -m bigquery_pipeline.cli predict-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
  -> ML.PREDICT
  -> 写入 ashare.ads_signal_ml_stock_picker_bqml_1d
```

```text
python -m bigquery_pipeline.cli audit-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
  -> 校验模型、评估指标、预测表和每日 Top-N 上限
```

## 6. 配置变更

新增配置：

```yaml
bqml:
  ml_stock_picker:
    model_name: "bqml_ml_stock_picker_baseline"
    prediction_table: "ads_signal_ml_stock_picker_bqml_1d"
    train_start_date: "20180101"
    train_end_date: "20241231"
    eval_start_date: "20250101"
    eval_end_date: "20251231"
    prediction_start_date: "20250101"
    prediction_end_date: "20260522"
    label_horizon: 5
    top_pct: 0.30
    bottom_pct: 0.30
    top_n: 50
    max_iterations: 30
    learn_rate: 0.05
    max_tree_depth: 6
    subsample: 0.8
```

## 7. 不可改动的红线区域

- 不修改 `strategy/ml_stock_picker` 下单逻辑。
- 不把 BigQuery ML 预测表直接接入实盘或虚拟盘。
- 不删除或重建任何 DWS 源表。
- 不把未来 `label_return` 作为预测特征。
- 不把模型输出保存为本地 `.pkl`，BigQuery ML 模型留在 BigQuery model object 中。
- 不把本需求代码放入 `gcs_to_bigquery/`。

## 8. 修改范围与位置

| 文件 | 修改位置 | 修改内容 |
| --- | --- | --- |
| `bigquery_pipeline/bqml.py` | 新文件 | BQML 训练 SQL、预测 SQL、审计逻辑 |
| `bigquery_pipeline/cli.py` | parser 和 command 分发 | 新增 `train-bqml-ml-stock-picker`、`predict-bqml-ml-stock-picker`、`audit-bqml-ml-stock-picker` |
| `bigquery_pipeline/config.yaml` | `bqml.ml_stock_picker` | 新增 baseline 参数 |
| `tests/test_bigquery_pipeline_bqml.py` | 新测试 | 校验 SQL 口径和 CLI |
| `ARCHITECTURE.md` | BigQuery 分层/文件清单 | 同步 baseline 状态 |

不修改：

- `gcs_to_bigquery/*`
- `data_transfer/*`
- `engine/*`
- `account/*`
- `strategy/*`

## 9. 验收标准

1. 训练命令成功：
   - BigQuery 中存在 `data-aquarium.ashare.bqml_ml_stock_picker_baseline` model。

2. 预测命令成功：
   - BigQuery 中存在 `data-aquarium.ashare.ads_signal_ml_stock_picker_bqml_1d`。
   - 表非空。
   - 字段包含 `equity_code`、`date`、`prob_up`、`score_rank`、`is_selected`、`signal`。

3. 审计命令成功：
   - 能输出 `ML.EVALUATE` 指标。
   - 每日 `is_selected = TRUE` 的数量不超过 `top_n`。

4. 测试：
   - 新增 BQML 单测通过。
   - 既有 BigQuery pipeline 单测不失败。

## 10. 备注

- baseline 使用 BigQuery ML boosted tree classifier，优点是无需本地高内存机器；缺点是模型对象留在 BigQuery 内，不直接兼容 `strategy/ml_stock_picker/models/*.pkl`。
- 后续若要让回测直接使用 BQML 预测结果，应另写 PRD，把 `ads_signal_ml_stock_picker_bqml_1d` 接入策略，并明确信号生成日期和 next-open 成交口径。
