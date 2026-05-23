# DWD 转换与 BigQueryDataSource 适配 PRD

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| LLM 型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-24 01:40:00 |
| 文档编号 | PRD_20260523_12 |
| 关联 Commit | 09ae287；本次按用户决策修订 |
| 需求优先级 | P0 |
| 所属拆分 | PRD_20260523_06 拆分子项 6/6 |
| 依赖 | PRD_20260523_11 已完成，`ashare.ods_*` 装载并 audit 通过 |
| 取代 | PRD_20260523_05 |

## 2. 背景与动机

PRD_11 完成后，当前状态应为：

- GCS 输入源仍为 `gs://data-aquarium/a-share/standardized_parquet/`
- ODS 表位于 `data-aquarium.ashare.ods_*`，类型为 BigQuery external table over GCS Parquet
- ODS 保留贴源 schema，不复制业务数据，不要求全部英文严格类型
- `data-aquarium.ashare.dwd_*` 仍待生成

本 PRD 完成最后的可回测数据层：

```text
ashare.ods_* external table -> ashare.dwd_* native table -> BigQueryDataSource -> run_backtest.py
```

用户已确认旧 BigQuery dataset 不作为 rollback。PRD_12 的验收不能依赖旧 `ashare_core` 对照回测，而应以 DWD 内部质量、GCS/ODS/DWD 对账和同参重复回测一致性为准。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
| --- | --- | --- |
| `data_layer` | 是 | `BigQueryDataSource` 读取 `ashare` dataset 下的 `dwd_*` 表 |
| `engine` / `account` / `strategy` / `analytics` / `utils` | 否 | 不修改 |
| `config` | 是 | 确认 `config/backtest.yaml` 的 dataset 和表名映射 |
| `CLI` | 是 | 新增或确认 `transform-dwd`、`audit-dwd`、`smoke-query` |

是否影响回测结果可复现性：是。本 PRD 是本系列最终回测验收点。

## 4. 关键文件路径与现有函数签名

```python
# data_layer/bigquery_source.py
def get_stock_list(self) -> pd.DataFrame:
    """从 dim_security 获取股票列表。"""
```

```python
# data_layer/bigquery_source.py
def _fetch_daily_bars(
    self,
    code: str,
    start_date: str,
    end_date: str,
    adjust: str,
) -> pd.DataFrame:
    """日K查询。根据代码类型自动路由到 equity / fund / index 表。"""
```

```python
# gcs_to_bigquery/pipeline.py
def merge_table(config: dict, target_table: str) -> None:
    """[deprecated] MERGE staging 表到 core 表"""
```

## 5. 需求详情

### 5.1 功能目标

- 生成 P0 DWD 表：
  - `ashare.dwd_fact_equity_kline_1d`
  - `ashare.dwd_fact_fund_kline_1d`
  - `ashare.dwd_fact_index_kline_1d`
  - `ashare.dwd_fact_board_component_1d`
  - `ashare.dwd_dim_security`
- DWD 股票事实表使用 `equity_code`。
- `dwd_dim_security` 继续使用 `security_code`。
- 基金、指数、板块事实表分别使用 `fund_code`、`index_code`、`board_code`。
- 实现 `transform-dwd`：从 `ashare.ods_*` 转换为 `ashare.dwd_*`。
- 实现 `audit-dwd`：字段、类型、主键、空值、分区一致性、行数对账。
- 实现 `smoke-query`：验证 `BigQueryDataSource` 可读。
- 完成同参重复回测一致性验证。

### 5.2 DWD schema

以股票日 K 为例：

```yaml
dwd_tables:
  fact_equity_kline_1d:
    destination_table: "dwd_fact_equity_kline_1d"
    source_ods_table: "ods_fact_equity_kline_1d"
    primary_key: ["equity_code", "date", "adjust_type"]
    partition_field: "partition_month"
    clustering_fields: ["equity_code", "adjust_type"]
    schema:
      - {name: "date", type: "DATE", mode: "REQUIRED"}
      - {name: "partition_month", type: "INT64", mode: "REQUIRED"}
      - {name: "equity_code", type: "STRING", mode: "REQUIRED"}
      - {name: "adjust_type", type: "STRING", mode: "REQUIRED"}
      - {name: "open", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "high", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "low", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "close", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "volume", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "amount", type: "NUMERIC", mode: "NULLABLE"}
      - {name: "source_file", type: "STRING", mode: "REQUIRED"}
      - {name: "source_hash", type: "STRING", mode: "NULLABLE"}
      - {name: "ingested_at", type: "TIMESTAMP", mode: "REQUIRED"}
```

`dwd_dim_security` 示例：

```yaml
dwd_tables:
  dim_security:
    destination_table: "dwd_dim_security"
    primary_key: ["security_code"]
    clustering_fields: ["security_code", "security_type"]
```

### 5.3 ODS 到 DWD transform

股票日 K 示例：

```sql
CREATE OR REPLACE TABLE `data-aquarium.ashare.dwd_fact_equity_kline_1d`
PARTITION BY RANGE_BUCKET(partition_month, GENERATE_ARRAY(199001, 209912, 1))
CLUSTER BY equity_code, adjust_type
AS
WITH normalized AS (
  SELECT
    SAFE.PARSE_DATE('%Y-%m-%d', date_raw) AS date,
    partition_month,
    COALESCE(equity_code, security_code, `股票代码`, `证券代码`, `代码`) AS raw_equity_code,
    COALESCE(adjust_type, 'none') AS adjust_type,
    SAFE_CAST(open_raw AS NUMERIC) AS open,
    SAFE_CAST(high_raw AS NUMERIC) AS high,
    SAFE_CAST(low_raw AS NUMERIC) AS low,
    SAFE_CAST(close_raw AS NUMERIC) AS close,
    SAFE_CAST(volume_raw AS NUMERIC) AS volume,
    SAFE_CAST(amount_raw AS NUMERIC) AS amount,
    source_file,
    source_hash,
    ingested_at
  FROM `data-aquarium.ashare.ods_fact_equity_kline_1d`
),
ranked AS (
  SELECT
    date,
    partition_month,
    NORMALIZE_EQUITY_CODE(raw_equity_code) AS equity_code,
    adjust_type,
    open, high, low, close, volume, amount,
    source_file, source_hash, ingested_at,
    ROW_NUMBER() OVER (
      PARTITION BY NORMALIZE_EQUITY_CODE(raw_equity_code), date, adjust_type
      ORDER BY ingested_at DESC, source_hash DESC
    ) AS rn
  FROM normalized
  WHERE date IS NOT NULL AND raw_equity_code IS NOT NULL
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1;
```

说明：

- SQL 中的 `NORMALIZE_EQUITY_CODE` 可以由 SQL UDF 或 Python 生成 SQL 表达式实现。
- 具体 ODS 源字段候选来自 PRD_09 的 `field_mappings`。
- 对 ODS 中不存在的候选字段，生成 SQL 时必须跳过，不能生成无效 SQL。
- 财务表的可见日期必须来自公告日期字段；`report_period` 只能作为报告期，不得作为回测可见日期。

### 5.4 BigQueryDataSource 适配

- `BigQueryDataSource` 默认 dataset：`ashare`。
- 股票日 K 表名：`dwd_fact_equity_kline_1d`。
- 股票代码过滤字段：`equity_code`。
- 返回给框架的统一列仍为 `code`。

示例 SQL：

```sql
SELECT equity_code AS code, date, open, high, low, close, volume, amount
FROM `data-aquarium.ashare.dwd_fact_equity_kline_1d`
WHERE equity_code = '000001.SZ'
  AND adjust_type = 'qfq'
  AND partition_month IN (202401, 202402, 202403)
  AND date BETWEEN '2024-01-01' AND '2024-03-31'
ORDER BY date;
```

`get_stock_list()` 示例：

```sql
SELECT security_code AS code, security_name AS name, list_date, industry
FROM `data-aquarium.ashare.dwd_dim_security`
WHERE security_type = 'stock'
  AND is_active = TRUE
ORDER BY security_code;
```

### 5.5 交互流程

```text
PRD_11 audit-ods PASS（ODS external table 可查）
  -> transform-dwd 生成 ashare.dwd_*
  -> audit-dwd PASS
  -> smoke-query PASS
  -> double_ma 重复回测一致
  -> ARCHITECTURE.md / WORK_SUMMARY 同步
  -> 用户确认后删除旧 BigQuery dataset
```

## 6. 配置变更

| 配置项 | 类型 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `dataset` | STRING | `ashare` | 单 dataset |
| `table_prefixes.dwd` | STRING | `dwd_` | DWD 表名前缀 |
| `dwd_tables.<table>.destination_table` | STRING | 按表 | DWD 目标表 |
| `dwd_tables.<table>.source_ods_table` | STRING | 按表 | ODS 来源表 |
| `dwd_tables.<table>.primary_key` | LIST | 按表 | 去重主键 |
| `dwd_tables.<table>.code_column` | STRING | 按表 | 资产代码字段 |
| `defaults.dwd.audit.null_rate_threshold` | FLOAT | `0.001` | 类型转换空值率阈值 |

`config/backtest.yaml` 必须为：

```yaml
data:
  bigquery:
    dataset: "ashare"
    tables:
      kline_1d_equity: "dwd_fact_equity_kline_1d"
      kline_1d_fund: "dwd_fact_fund_kline_1d"
      kline_1d_index: "dwd_fact_index_kline_1d"
      dim_security: "dwd_dim_security"
      board_component: "dwd_fact_board_component_1d"
```

## 7. 不可改动的红线区域

- 不依赖旧 `ashare_core` 作为 rollback 或验收前提。
- 不删除 `gs://data-aquarium/a-share/standardized_parquet/`。
- 不修改 `BaseDataSource.get_bars()` 接口签名。
- 不修改返回列语义：`code, date, open, high, low, close, volume, amount`。
- 不修改 `engine/`、`account/`、`strategy/`。
- 股票事实表不再使用 `security_code` 作为主代码字段。
- `dim_security` 不改成 `dim_equity`，也不把 `security_code` 改成 `equity_code`。
- `audit-dwd` 任一项失败时，不允许声明完成。

## 8. 修改范围与位置

| 文件 | 修改位置 | 修改内容 |
| --- | --- | --- |
| `gcs_to_bigquery/pipeline.py` | 新增 `transform_dwd()` / `audit_dwd()` / `smoke_query()` | DWD 转换和验收 |
| `gcs_to_bigquery/config.yaml` | `dwd_tables` / `field_mappings` | DWD 表配置 |
| `data_layer/bigquery_source.py` | `_resolve_kline_table()` / `_fetch_daily_bars()` / `get_stock_list()` | dataset 和字段适配 |
| `tests/test_bigquery_dwd_transform.py` | 新增 | transform SQL、字段映射、审计测试 |
| `tests/test_bigquery_source.py` | 如已有 | BigQueryDataSource 表名与字段测试 |
| `ARCHITECTURE.md` | BigQuery 数据状态 | 同步 DWD 已可回测 |

不修改：`data_transfer/*`、`engine/*`、`account/*`、`strategy/*`。

## 9. 验收标准

### 9.1 DWD 表

P0 DWD 表全部存在且行数 > 0：

- `ashare.dwd_fact_equity_kline_1d`
- `ashare.dwd_fact_fund_kline_1d`
- `ashare.dwd_fact_index_kline_1d`
- `ashare.dwd_fact_board_component_1d`
- `ashare.dwd_dim_security`

### 9.2 DWD schema

- `dwd_fact_equity_kline_1d` 包含 `equity_code`，不以 `security_code` 作为股票主代码字段。
- `dwd_dim_security` 包含 `security_code`。
- `date` 类型为 `DATE`。
- `open/high/low/close/volume/amount` 类型为 `NUMERIC` 或明确可接受的数值类型。
- `partition_month` 类型为 `INT64`。

### 9.3 audit-dwd

`audit-dwd` 必须输出 PASS：

- 表存在。
- DWD 行数 <= ODS 行数。
- 主键唯一。
- 关键字段非空。
- 类型转换空值率在阈值内。
- `partition_month` 与 `date` 一致。

### 9.4 smoke-query

`smoke-query` 必须通过：

- `get_stock_list()` 返回非空。
- `get_bars('510300.SH', '20240101', '20240331', period='daily', adjust='qfq')` 返回非空。
- 返回列为 `[code, date, open, high, low, close, volume, amount]`。
- `date` 返回 `YYYYMMDD` 字符串。
- 查询使用 `partition_month` 过滤。

### 9.5 回测可复现

同参运行两次：

```bash
python run_backtest.py --strategy double_ma --start 20240101 --end 20240331 \
  --symbol 510300.SH --capital 100000 --data-source bigquery
```

预期：

- 两次交易次数完全一致。
- 两次最终资产完全一致或在浮点误差容忍范围内。
- 两次最大回撤完全一致或在浮点误差容忍范围内。
- 若不一致，必须定位到数据、排序、去重或类型转换原因，不允许声明完成。

### 9.6 成本收尾

在 `audit-dwd`、`smoke-query`、回测可复现全部通过后，经用户明确确认，允许删除：

- `ashare_raw`
- `ashare_core`
- `ashare_mart`

不得删除：

- `gs://data-aquarium/a-share/standardized_parquet/`

### 测试用例输入输出

**用例 1：股票字段**

- 输入：`SELECT column_name FROM ashare.INFORMATION_SCHEMA.COLUMNS WHERE table_name='dwd_fact_equity_kline_1d'`
- 预期：包含 `equity_code`；不要求包含 `security_code`

**用例 2：维表字段**

- 输入：`SELECT column_name FROM ashare.INFORMATION_SCHEMA.COLUMNS WHERE table_name='dwd_dim_security'`
- 预期：包含 `security_code`

**用例 3：BigQueryDataSource**

- 输入：`get_bars('510300.SH', '20240101', '20240131')`
- 预期：返回非空 DataFrame，统一代码列名为 `code`

## 10. 备注

- 本 PRD 不再与旧 `ashare_core` 做对照回测，因为旧 dataset 不作为 rollback 保留。
- 若需要历史对照，只能在旧 dataset 删除前临时执行，不作为本 PRD 必需验收项。
- 当前 GCS prefix 是长期可重装载输入源，不能随着旧 BigQuery dataset 一起删。
