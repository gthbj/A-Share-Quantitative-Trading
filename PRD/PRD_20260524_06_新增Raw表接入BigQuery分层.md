# PRD_20260524_06 新增 Raw 表接入 BigQuery 分层

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-24 12:22:33 |
| 文档编号 | PRD_20260524_06 |
| 关联 Commit | 待提交 |
| 需求优先级 | P0 |

## 2. 背景与动机

新增 raw 目录已经在 VM 上标准化并上传到当前正式 GCS 前缀：

```text
gs://data-aquarium/a-share/standardized_parquet/
```

新增表包括开盘啦榜单、龙虎榜席位、资金流向、指数/行业基础信息、指数成分、行业成分、行业指数行情和指数估值指标等。当前 BigQuery `ashare` dataset 已有旧 36 张表的 ODS/DWD/DWS/ADS 第一版，但新增标准化表尚未进入 BigQuery 分层。

不处理会导致：

- GCS 上已有的新增数据无法通过 BigQuery 查询。
- ODS manifest、ODS external table 与当前 GCS 实际对象不一致。
- DWD/DWS/ADS 不能消费资金流、龙虎榜、开盘啦和行业指数特征。
- `bigquery_pipeline/` 仍未承接完整 DWD/DWS/ADS 逻辑，和“GCS 到 BigQuery 与 BigQuery 内部分层处理分目录”的设计不一致。

期望行为：不重跑 VM、不创建 `standardized_parquet_v2/`，直接使用当前 GCS 正式前缀，完成 ODS external table 更新、DWD native table 生成、DWS 事件特征表和 ADS 事件信号表生成，并运行 audit。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `gcs_to_bigquery` | 是 | 只负责 manifest、ODS external table 与 ODS audit，不承载 DWD/DWS/ADS 内部转换。 |
| `bigquery_pipeline` | 是 | 承接完整 BigQuery 内部分层处理：ODS -> DWD -> DWS -> ADS。 |
| `config` | 是 | 使用现有 `ashare` 单 dataset 与 `ods_`/`dwd_`/`dws_`/`ads_` 表前缀。 |
| `data_layer` | 不影响 | 本次不改变 `BigQueryDataSource` 接口和默认读取表。 |
| `strategy` | 不影响 | 本次只生成 ADS 候选信号，不接入下单。 |
| `engine` / `account` / `analytics` / `utils` | 不影响 | 不修改。 |

是否影响回测结果可复现性：间接影响。新增 DWS/ADS 表是数据资产，不改变现有回测执行路径；如后续策略读取新增 ADS 信号，需另写策略接入 PRD。

## 4. 关键文件路径与现有函数签名

```python
# gcs_to_bigquery/pipeline.py
def create_ods_external(config: dict) -> None:
    client = bq_client(config)
    ods_tables = resolve_ods_external_tables(config)
```

```python
# bigquery_pipeline/cli.py
def main() -> int:
    parser = argparse.ArgumentParser(description="Run BigQuery-internal A-share DWD/DWS/ADS pipeline tasks.")
```

```python
# bigquery_pipeline/dwd.py
def transform_dwd(config: dict, target_table: str | None = None) -> None:
    normalized = (target_table or "fact_financial_indicator").removeprefix("dwd_")
```

```python
# bigquery_pipeline/dws.py
def transform_dws(config: dict, target_table: str | None = None) -> None:
    selected = _select(DWS_TRANSFORMS, target_table, "dws")
```

```python
# bigquery_pipeline/ads.py
def transform_ads(config: dict, target_table: str | None = None) -> None:
    raise RuntimeError(
        "ADS transforms have been split out of gcs_to_bigquery. "
        "No ADS rebuild is required for the financial indicator fix."
    )
```

## 5. 需求详情

### 功能目标

1. 重新扫描当前 GCS `standardized_parquet/`，manifest 必须覆盖旧 36 张表和新增 12 张表。
2. 通过 `gcs_to_bigquery/pipeline.py` 更新 `ashare.ods_*` external table；ODS 仍然只做贴源 external table，不复制业务数据。
3. 将完整 DWD/DWS/ADS 内部转换迁入 `bigquery_pipeline/`：
   - `gcs_to_bigquery/` 保留 ODS external table 和 legacy 命令。
   - `bigquery_pipeline/` 负责所有 BigQuery 内部 SQL transform 与 audit。
4. 新增 raw 表 DWD 目标：
   - `dwd_fact_kpl_board_1d`
   - `dwd_fact_dragon_tiger_seat_1d`
   - `dwd_fact_money_flow_1d`
   - `dwd_dim_index_profile`
   - `dwd_fact_index_component_1d`
   - `dwd_dim_citic_industry`
   - `dwd_fact_citic_industry_component_history`
   - `dwd_dim_sw_industry`
   - `dwd_fact_sw_industry_component_1d`
   - `dwd_fact_citic_industry_kline_1d`
   - `dwd_fact_sw_industry_kline_1d`
   - `dwd_fact_index_market_indicator_1d`
5. 新增 DWS 表：
   - `dws_equity_event_money_flow_features_1d`
     - 来源：`dwd_fact_kpl_board_1d`、`dwd_fact_dragon_tiger_seat_1d`、`dwd_fact_money_flow_1d`
     - 粒度：`equity_code + date`
     - 用途：事件驱动、资金流和龙虎榜特征。
6. 新增 ADS 表：
   - `ads_signal_event_money_flow_1d`
     - 来源：`dws_equity_event_money_flow_features_1d`
     - 规则：按日期对净流入、龙虎榜净买入、连板和开盘啦事件加权生成 `score_proxy`，给出每日候选信号。
7. 继续保留并可重跑已有 DWS/ADS：
   - `dws_equity_daily_features`
   - `dws_fund_daily_features`
   - `dws_index_daily_features`
   - `dws_portfolio_asset_returns_1d`
   - `dws_board_component_latest`
   - `dws_pair_candidate_stats`
   - `dws_equity_valuation_features`
   - `dws_equity_fundamental_features`
   - 现有 5 张 ADS 策略候选表。

### 交互流程

```text
当前 GCS standardized_parquet/
  -> gcs_to_bigquery manifest
  -> gcs_to_bigquery init-ods / create-ods-external / sync-manifest / audit-ods
  -> bigquery_pipeline transform-dwd --table <新增表> 或全量
  -> bigquery_pipeline audit-dwd
  -> bigquery_pipeline transform-dws
  -> bigquery_pipeline audit-dws
  -> bigquery_pipeline transform-ads
  -> bigquery_pipeline audit-ads
  -> ARCHITECTURE.md / README 同步
```

失败时：

```text
某一层 audit 失败
  -> 不声明完成
  -> 保留已成功生成的上游层
  -> 修复 SQL / 字段映射
  -> 重跑失败表及其下游表
```

## 6. 配置变更

沿用：

```yaml
project_id: "data-aquarium"
location: "asia-east2"
dataset: "ashare"
gcs:
  bucket: "data-aquarium"
  prefix: "a-share/standardized_parquet"
table_prefixes:
  ods: "ods_"
  dwd: "dwd_"
  dws: "dws_"
  ads: "ads_"
```

新增或确认：

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `defaults.ads.event_money_flow_top_n` | INT | `100` | 每日事件/资金流 ADS 候选数量。 |
| `defaults.dwd.required_full_coverage` | BOOL | `true` | DWD audit 要求所有 ODS 业务表有对应 DWD 表。 |

## 7. 不可改动的红线区域

- 不重生成 Parquet。
- 不创建 `standardized_parquet_v2/`。
- 不删除或修改 `gs://data-aquarium/a-share/standardized_parquet/`。
- 不删除或修改 GCS raw 源数据。
- 不启动已经停止的 `ashare-parquet-worker`。
- 不把 ODS 业务数据复制成 native 表；ODS 继续使用 external table。
- 不把 BigQuery 内部 DWD/DWS/ADS 逻辑继续新增到 `gcs_to_bigquery/`。
- 不修改 `BaseDataSource`、`BigQueryDataSource` 对外接口。
- 不让 ADS 信号直接触发实盘或虚拟盘交易。
- 财务和基本面数据继续使用公告日期作为可见日期，不使用 `report_period` 作为可见日期。

## 8. 修改范围与位置

### 主要修改文件

| 文件 | 修改内容 |
|---|---|
| `PRD/PRD_20260524_06_新增Raw表接入BigQuery分层.md` | 本 PRD。 |
| `bigquery_pipeline/sql.py` | 补 BigQuery SQL 生成公共函数。 |
| `bigquery_pipeline/dwd.py` | 承接完整 ODS -> DWD transform/audit，并补新增 raw 表专用 DWD SQL。 |
| `bigquery_pipeline/dws.py` | 承接策略 DWS 与新增事件资金流 DWS。 |
| `bigquery_pipeline/ads.py` | 承接策略 ADS 与新增事件资金流 ADS。 |
| `bigquery_pipeline/cli.py` | 保持统一 CLI 命令入口。 |
| `bigquery_pipeline/config.yaml` | 补 ADS 事件候选配置。 |
| `tests/` | 增加 SQL 生成、CLI 和新增表映射测试。 |
| `ARCHITECTURE.md` | 同步当前 BigQuery 分层状态。 |
| `gcs_to_bigquery/README.md` | 明确 GCS->ODS 与 BigQuery 内部处理边界。 |

### 不修改的文件

- `data_transfer/*`
- `engine/*`
- `account/*`
- `strategy/*`
- `data_layer/*` 对外接口

## 9. 验收标准

### 9.1 ODS

- `gcs_to_bigquery manifest` 扫描成功。
- manifest 表数包含新增 12 张表。
- `create-ods-external` 更新成功。
- `audit-ods` 通过。

测试用例：

- 输入：`gs://data-aquarium/a-share/standardized_parquet/fact_money_flow_1d/`
- 预期：存在 `ashare.ods_fact_money_flow_1d` external table，且 sample query 返回非空。

### 9.2 DWD

- 新增 12 张 DWD 表全部存在且非空。
- 以下关键字段必须存在：
  - `dwd_fact_kpl_board_1d`: `equity_code`、`date`、`partition_month`
  - `dwd_fact_dragon_tiger_seat_1d`: `equity_code`、`date`、`net_amount`
  - `dwd_fact_money_flow_1d`: `equity_code`、`date`、`net_inflow_amount`
  - `dwd_fact_index_component_1d`: `index_code`、`equity_code`、`date`
  - `dwd_fact_citic_industry_kline_1d`: `industry_code`、`date`、`close`
  - `dwd_fact_sw_industry_kline_1d`: `industry_code`、`date`、`close`

测试用例：

- 输入：ODS 中 `资金流向/20260522.csv` 对应行。
- 预期：DWD 中同一股票同一日期存在 `equity_code` 标准化为 `000001.SZ` 格式，`net_inflow_amount` 可数值化。

### 9.3 DWS

- `dws_equity_event_money_flow_features_1d` 存在且非空。
- 至少包含：
  - `equity_code`
  - `date`
  - `net_inflow_amount`
  - `dragon_tiger_net_amount`
  - `is_kpl_event`
  - `limit_up_streak`

测试用例：

- 输入：同一 `equity_code + date` 同时存在资金流和龙虎榜记录。
- 预期：DWS 聚合到一行，两个来源的金额字段不互相覆盖。

### 9.4 ADS

- `ads_signal_event_money_flow_1d` 存在且非空。
- 每日候选数不超过 `event_money_flow_top_n`。
- `score_proxy` 不使用未来收益。

测试用例：

- 输入：某日 DWS 中 `net_inflow_amount`、`dragon_tiger_net_amount` 均较高的股票。
- 预期：ADS 中该股票 `score_rank` 更靠前，`is_selected = TRUE`。

### 9.5 回测验证要求

本 PRD 不要求把新增 ADS 信号接入回测下单。回测可复现性只要求：

- 现有 `BigQueryDataSource` smoke query 继续通过。
- 同一配置重复执行现有 `double_ma` preset，不因新增表接入而改变既有读取链路。

## 10. 备注

- 本需求只使用当前这一版 GCS 数据。
- 新增 DWD/DWS/ADS 优先保证“可查、可审计、无未来函数”，后续策略接入再细化信号权重。
- 如果某些 ODS external table 因源字段名不合法使用 `raw_col_*` 安全 schema，DWD 必须通过列位置兜底，不得阻塞整层生成。
