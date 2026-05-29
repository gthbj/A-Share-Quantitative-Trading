# PRD_20260527_02 Tushare Raw 数据构建 BigQuery `ashare_ods` 层

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM 型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-27 CST |
| 文档编号 | PRD_20260527_02 |
| 关联 Commit | 待提交 |
| 需求优先级 | P0 |

## 2. 背景

GCS 上已存在 Tushare 原始采集数据：

```text
gs://data-aquarium/a-share/tushare/raw_data/
  api=<采集接口或实现名>/
    endpoint=<接口名或接口名_参数>/
      partition_date=<YYYYMMDD>/
        data.parquet
```

本 PRD 为这部分数据建立一套全新的 BigQuery ODS 外部表，落到全新独立 dataset `ashare_ods`。

与现有 `ashare` dataset、`standardized_parquet/` 外部表、以及任何旧 ODS 表均无依赖关系。本 PRD 不复用、不修改旧管道。

## 3. 事实基线（GCS Inventory）

`raw_data/` 下共 36 个 `api=` 目录。按 endpoint 结构分四类：

### 3.1 单 endpoint api（一对一直接建表）

```text
adj_factor, ci_daily, daily, daily_basic, daily_info, disclosure_date,
dividend, index_classify, limit_list_d, margin, margin_detail, moneyflow,
namechange, new_share, st, stk_holdernumber, stk_limit, stock_st,
suspend_d, sw_daily, sz_daily_info, top10_floatholders, top10_holders,
trade_cal
```

### 3.2 `*_vip` 实现 api（去 vip 后缀建表）

```text
api=balancesheet_vip/endpoint=balancesheet/
api=cashflow_vip/endpoint=cashflow/
api=express_vip/endpoint=express/
api=fina_indicator_vip/endpoint=fina_indicator/
api=forecast_vip/endpoint=forecast/
api=income_vip/endpoint=income/
```

### 3.3 参数化 endpoint api（合表）

每个 api 下多个 endpoint，仅 Tushare 请求参数不同：

| api | endpoint 数量 | endpoint 形态 |
|---|---|---|
| `index_daily` | 7 | `index_daily_<index_code>_<exchange>` |
| `index_dailybasic` | 5 | `index_dailybasic_<index_code>_<exchange>` |
| `index_weight` | 7 | `index_weight_<index_code>_<exchange>` |

### 3.4 分交易所 endpoint api（schema audit 后决定合或拆）

```text
api=stock_basic/endpoint=stock_basic_{listed, delisted}/
api=stock_company/endpoint=stock_company_{sse, szse, bse}/
api=margin_secs/endpoint=margin_secs_{sse, szse, bse}/
```

## 4. 设计原则

1. `api=` 是采集路径/实现名；`endpoint=` 是源接口语义。表名取归一化后的 `endpoint` 基础接口名，不把 `_vip`、指数代码、交易所、上市状态写进主表名。
2. 参数化 endpoint 合表，用 `ts_code` 数据列 / `endpoint` partition 列 / `_request_params_json` 数据列区分。
3. 同一接口下不同分组（如 sse / szse / bse）默认合表；仅当 schema audit 证实不兼容时才拆。
4. ODS 保留原始字段，不做类型清洗、不做去重、不做业务转换。
5. dataset 独立为 `ashare_ods`，物理隔离，不与任何现有 dataset 共表名。

## 5. ODS 表命名规则

```text
ods_tushare_<normalized_endpoint>
```

归一化规则：

| GCS endpoint 模式 | normalized_endpoint | 示例 ODS 表 |
|---|---|---|
| 与 api 同名 | endpoint 名 | `ods_tushare_daily` |
| api 名为 `<base>_vip`，endpoint 为 `<base>` | base | `ods_tushare_income` |
| `<base>_<index_code>_<exchange>` | base | `ods_tushare_index_daily` |
| `<base>_<exchange>` | base | `ods_tushare_stock_company` |
| `<base>_<list_status>` | base | `ods_tushare_stock_basic` |

## 6. ODS 表清单（目标 36 张）

### 6.1 单 endpoint 表（24 张）

```text
ods_tushare_adj_factor
ods_tushare_ci_daily
ods_tushare_daily
ods_tushare_daily_basic
ods_tushare_daily_info
ods_tushare_disclosure_date
ods_tushare_dividend
ods_tushare_index_classify
ods_tushare_limit_list_d
ods_tushare_margin
ods_tushare_margin_detail
ods_tushare_moneyflow
ods_tushare_namechange
ods_tushare_new_share
ods_tushare_st
ods_tushare_stk_holdernumber
ods_tushare_stk_limit
ods_tushare_stock_st
ods_tushare_suspend_d
ods_tushare_sw_daily
ods_tushare_sz_daily_info
ods_tushare_top10_floatholders
ods_tushare_top10_holders
ods_tushare_trade_cal
```

### 6.2 `*_vip` 去后缀（6 张）

```text
ods_tushare_balancesheet
ods_tushare_cashflow
ods_tushare_express
ods_tushare_fina_indicator
ods_tushare_forecast
ods_tushare_income
```

### 6.3 参数化 endpoint 合表（3 张）

```text
ods_tushare_index_daily
ods_tushare_index_dailybasic
ods_tushare_index_weight
```

### 6.4 分组合表（schema audit 后定，目标 3 张）

```text
ods_tushare_stock_basic     # listed + delisted
ods_tushare_stock_company   # sse + szse + bse
ods_tushare_margin_secs     # sse + szse + bse
```

合计目标 36 张。若 6.4 内任一组 audit 不通过则按 endpoint 拆，最终行数会略多。

### 6.5 待人工裁定的疑似重叠对

下列 api 对名字相近，需要在 audit 阶段确认是否同源、是否合表。默认结论先列在表格里，audit 后再 lock：

| api 对 | 默认结论 | 备注 |
|---|---|---|
| `st` vs `stock_st` | 拆，两表 | 看 schema 是否完全相同，相同则后续可考虑合表 |
| `daily_info` vs `sz_daily_info` | 拆，两表 | 沪深两个独立接口 |
| `ci_daily` vs `sw_daily` | 拆，两表 | 中信行业 / 申万行业，独立接口 |
| `top10_holders` vs `top10_floatholders` | 拆，两表 | 全部股东 / 流通股东，独立接口 |

## 7. Hive Partition 方案

### 7.1 关键设计

每张表绑定单一 `api=` 前缀。`api` 不作为 partition column（对单表来说是常数，做 partition column 没意义、还多一层 Hive 解析）。

partition columns 始终是：

```sql
endpoint STRING,
partition_date STRING
```

`hive_partition_uri_prefix` 下沉到 `api=<name>/` 一层。

### 7.2 建表示例：单 endpoint 表

```sql
CREATE OR REPLACE EXTERNAL TABLE `data-aquarium.ashare_ods.ods_tushare_daily`
WITH PARTITION COLUMNS (
  endpoint STRING,
  partition_date STRING
)
OPTIONS (
  format = 'PARQUET',
  uris = [
    'gs://data-aquarium/a-share/tushare/raw_data/api=daily/endpoint=daily/partition_date=YYYYMMDD/data.parquet',
    -- ...由 manifest 精确生成,逐文件枚举
  ],
  hive_partition_uri_prefix = 'gs://data-aquarium/a-share/tushare/raw_data/api=daily/',
  require_hive_partition_filter = false
);
```

### 7.3 建表示例：参数化 endpoint 合表

```sql
CREATE OR REPLACE EXTERNAL TABLE `data-aquarium.ashare_ods.ods_tushare_index_daily`
WITH PARTITION COLUMNS (
  endpoint STRING,
  partition_date STRING
)
OPTIONS (
  format = 'PARQUET',
  uris = [
    'gs://data-aquarium/a-share/tushare/raw_data/api=index_daily/endpoint=index_daily_*/partition_date=*/data.parquet'
  ],
  hive_partition_uri_prefix = 'gs://data-aquarium/a-share/tushare/raw_data/api=index_daily/',
  require_hive_partition_filter = false
);
```

### 7.4 其他建表要点

- `partition_date` 显式声明 STRING，避免 BigQuery AUTO 模式把 8 位 YYYYMMDD 推断为 INTEGER。DWD 再 `PARSE_DATE('%Y%m%d', partition_date)` 转 DATE。
- `require_hive_partition_filter = false`：ODS 允许全表扫描，高频查询走 DWD 视图。
- 单 endpoint 表的 `endpoint` 列对该表是常数，保留以让所有 `ods_tushare_*` 表 schema 形态一致。

## 8. URI 策略

### 8.1 单 endpoint 表：manifest 精确 URI（默认）

普通单 endpoint 表的累计文件数在 BigQuery 10,000 `source_uris` 上限内，使用 manifest 精确 URI，每次建表绑定到具体 GCS snapshot，便于审计和复现。

### 8.2 参数化 endpoint 表：受控 wildcard（必需）

参数化 endpoint 表的文件数估算超出 manifest 上限：

| 表 | endpoint 数 | 年数（估） | 文件估算 |
|---|---|---|---|
| `ods_tushare_index_daily` | 7 | 10 | 7 × 10 × 252 ≈ 17,640 |
| `ods_tushare_index_weight` | 7 | 10 | 同量级 |
| `ods_tushare_index_dailybasic` | 5 | 10 | 5 × 10 × 252 ≈ 12,600 |

均超 BigQuery `source_uris` 10,000 上限。**这三张表必须从第一版起使用受控 wildcard**，不能先 manifest 后改造。

允许的 wildcard 形态：

```text
gs://data-aquarium/a-share/tushare/raw_data/api=<api_name>/endpoint=<base>_*/partition_date=*/data.parquet
```

禁止使用跨 api 的 `**` wildcard。

每次建表写入 wildcard 实际匹配清单到 audit 目录，作为复现依据。

### 8.3 URI 总数监控

每张表建表前评估文件数：
- 估算公式：`endpoint 数 × 年数 × 252 × 文件/天`
- 超过 8,000 时切到 wildcard
- 超过 10,000 时 manifest 必然失败

## 9. Schema Audit（阻塞前置）

### 9.1 命令

```bash
python gcs_to_bigquery/audit_tushare_ods_schema.py \
  --gcs-prefix gs://data-aquarium/a-share/tushare/raw_data \
  --output audit/<timestamp>/schema_report.json
```

### 9.2 输出字段

```text
api_name
endpoint_key
normalized_endpoint
sampled_partition_date
sampled_file_uri
column_name
arrow_type
nullable
is_null_only_in_sample
audit_columns_present  # {_source, _tushare_api, _endpoint_key, _run_id, _ingested_at, _logical_date, _request_params_json}
schema_fingerprint
```

### 9.3 必须检查项

1. 同 `normalized_endpoint` 下，不同 `endpoint_key` 的同名列是否 arrow type 一致。
2. null-only Parquet 列的位置。这是 BigQuery PARQUET external table 合表查询时最常见的"建表不报错、查询才报错"陷阱。
3. BigQuery 非法字段名：含中文、`%`、`#`、起始字符为数字等。存在则建表必须用 `column_name_character_map = V2` 或 safe_schema 占位列兜底。
4. 审计列存在性矩阵：
   - `_source`、`_tushare_api`、`_endpoint_key`、`_run_id`、`_ingested_at`、`_logical_date`、`_request_params_json`
   - 缺失则 DWD 去重和参数解析必须 fallback 到 `endpoint` partition column + 正则。

### 9.4 决策规则

| audit 结果 | 行为 |
|---|---|
| 同 `normalized_endpoint` 下所有 endpoint schema 完全兼容 | 合表 |
| 仅 null-only 列差异 | 合表，并在配置里记录该列为可选 |
| 关键列 arrow type 不一致 | 拆表，命名加 endpoint 后缀，DWD 再 union |
| 字段名 BigQuery 非法 | 合表但启用 `safe_schema` 占位列方案 |

### 9.5 重点 audit 对象

```text
stock_basic_{listed, delisted}
stock_company_{sse, szse, bse}
margin_secs_{sse, szse, bse}
index_daily_*
index_dailybasic_*
index_weight_*
```

### 9.6 Audit 通过准入

- 每个目标 ODS 表对应 audit 报告中至少有一条记录。
- 6.4 三组分组合表表均有 lock 结论（合 / 拆）。
- 6.5 四对疑似重叠 api 均有 lock 结论。
- 所有非法字段名均有处理方案。

## 10. Dataset 建立

```sql
CREATE SCHEMA IF NOT EXISTS `data-aquarium.ashare_ods`
OPTIONS(
  location = '<待确认：与 gs://data-aquarium 同 region>',
  description = 'Tushare raw_data ODS 层。Append-only PARQUET external tables。'
);
```

location 必须与 GCS bucket region 兼容，否则 external table 建表报错。建 dataset 前必须先确认 bucket region（`gsutil ls -L -b gs://data-aquarium | grep -i location`）。

## 11. 配置

新增配置文件：

```text
gcs_to_bigquery/tushare_ashare_ods_config.yaml
```

骨架：

```yaml
project_id: data-aquarium
location: <region>
dataset: ashare_ods

gcs:
  bucket: data-aquarium
  prefix: a-share/tushare/raw_data

defaults:
  ods:
    source_format: PARQUET
    require_hive_partition_filter: false
    schema_audit_required: true
    column_name_character_map: V2  # 或 safe_schema

normalized_endpoints:
  daily:
    api_name: daily
    endpoint_patterns: [daily]
    destination_table: ods_tushare_daily
    uri_strategy: manifest

  income:
    api_name: income_vip
    endpoint_patterns: [income]
    destination_table: ods_tushare_income
    uri_strategy: manifest

  index_daily:
    api_name: index_daily
    endpoint_patterns: ["index_daily_*"]
    destination_table: ods_tushare_index_daily
    uri_strategy: controlled_wildcard
    wildcard_pattern: "endpoint=index_daily_*/partition_date=*/data.parquet"

  stock_basic:
    api_name: stock_basic
    endpoint_patterns: [stock_basic_listed, stock_basic_delisted]
    destination_table: ods_tushare_stock_basic
    uri_strategy: manifest
    split_on_schema_incompatible: true

  # ... 其余 32 张表配置同形态
```

## 12. DWD 映射（仅作记录，非本 PRD 落地范围）

避免命名漂移：

```text
ods_tushare_daily              -> dwd_fact_equity_kline_1d
ods_tushare_adj_factor         -> dwd_fact_adjust_factor
ods_tushare_daily_basic        -> dwd_fact_equity_daily_basic
ods_tushare_stock_basic        -> dwd_dim_security
ods_tushare_trade_cal          -> dwd_dim_trade_calendar
ods_tushare_income             -> dwd_fact_income_statement
ods_tushare_balancesheet       -> dwd_fact_balance_sheet
ods_tushare_cashflow           -> dwd_fact_cash_flow_statement
ods_tushare_fina_indicator     -> dwd_fact_financial_indicator
ods_tushare_forecast           -> dwd_fact_earnings_forecast
ods_tushare_express            -> dwd_fact_earnings_express
ods_tushare_index_daily        -> dwd_fact_index_kline_1d
ods_tushare_index_weight       -> dwd_fact_index_component_1d
```

DWD 阶段做：类型转换、`_run_id/_ingested_at` 去重、`_request_params_json` 解析、字段补齐 union。

## 13. 影响模块

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `gcs_to_bigquery` | 是 | 新增 `ashare_ods` 配置、audit 命令、建表命令 |
| `bigquery_pipeline` | 后续 | DWD 转换阶段会引用 `ods_tushare_*` |
| `data_ingestion` | 否 | 不修改采集器 |
| 现有 `ashare` dataset | 否 | 全新 dataset 物理隔离 |
| `standardized_parquet/` | 否 | 不读、不动 |
| `data_layer`、`strategy` | 否 | 本 PRD 不修改回测数据源接口 |

是否影响回测结果可复现性：**否**。本 PRD 不切换策略读取路径。

## 14. 不可改动的红线

- 不读、不动现有 `ashare` dataset 与 `standardized_parquet/` 外部表。
- 不删除 `raw_data/` 中任何对象。
- 不在 ODS 层做业务去重、类型清洗、字段重命名。
- 不在未通过 schema audit 的前提下合并参数化或分交易所 endpoint。
- 不使用跨 api 的 `**` wildcard。
- 不让 `ods_tushare_*` 与现有任何 dataset 中的同名表产生命名冲突。

## 15. 实施步骤

### 阶段 1 - Inventory 与 Schema Audit（阻塞前置）

1. Inventory 脚本：列出 36 个 api 下所有 endpoint × partition_date 组合，确认文件数估算。
2. Schema audit 脚本：每个 endpoint sample 1 个 partition_date 的 parquet，输出第 9.2 节字段。
3. 生成 audit 报告，重点核实第 9.5 节 6 组对象。
4. 同步裁定第 6.5 节 4 对疑似重叠 api。

### 阶段 2 - Dataset 与 P0 单 endpoint 表

1. 确认 GCS bucket region，创建 dataset `ashare_ods`。
2. 建 P0 单 endpoint 表（manifest 精确 URI）：
   - `ods_tushare_daily`
   - `ods_tushare_daily_basic`
   - `ods_tushare_adj_factor`
   - `ods_tushare_trade_cal`
   - `ods_tushare_income`
   - `ods_tushare_balancesheet`
   - `ods_tushare_cashflow`
   - `ods_tushare_fina_indicator`
3. 验证：`SELECT COUNT(*)` 可执行；`partition_date` 列类型为 STRING；`bq show` 显示 externalDataConfiguration 含 hive partitioning。

### 阶段 3 - 参数化 endpoint 表

1. 建 `ods_tushare_index_daily`、`ods_tushare_index_dailybasic`、`ods_tushare_index_weight`（受控 wildcard）。
2. 验证：`SELECT endpoint, COUNT(*) GROUP BY endpoint` 能正确分组到各 endpoint。
3. 写入 wildcard 匹配清单到 audit 目录。

### 阶段 4 - 分组待 audit 表

按 audit 结论建 `ods_tushare_stock_basic` / `_stock_company` / `_margin_secs`，合或拆。

### 阶段 5 - 其余 P1 表

补齐 36 张目标表的剩余部分。

## 16. 验收标准

### 16.1 Dataset

- `data-aquarium.ashare_ods` 已创建。
- location 与 GCS bucket region 兼容。

### 16.2 Schema Audit

- 36 个 api、所有 endpoint 均产出 audit 记录。
- 6 组重点 audit 对象有合/拆 lock 结论。
- 4 对疑似重叠 api 有人工裁定记录。
- 所有非法字段名有处理方案。

### 16.3 ODS

- P0 8 张单 endpoint 表建表成功并可查。
- 参数化 endpoint 3 张表建表成功并可查。
- 分组 3 组表按 audit 结论落地。
- 所有表 `partition_date` 为 STRING。
- `bq show` 显示 hive partitioning prefix 下沉到 `api=<name>/`。
- 单表 `source_uris` 不超过 10,000；wildcard 表配套有匹配清单记录。

### 16.4 不破坏现状

- 现有 `ashare` dataset 无 schema 变化。
- 现有 `standardized_parquet/` 相关 external table 查询行为不变。

## 17. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 参数化 endpoint 文件数超 10k | manifest 建表失败 | 受控 wildcard 一开始就用，不留过渡 |
| 同 `normalized_endpoint` 下 schema 不兼容 | 查询时炸 | 阻塞前置 schema audit，不兼容就拆 |
| Parquet null-only 列 | 合表后查询特定 partition 失败 | audit 标识 null-only 列，必要时拆或在配置里登记可选列 |
| BigQuery 非法字段名 | 建表或查询失败 | 启用 `column_name_character_map = V2` 或 safe_schema 占位列 |
| 采集器审计列缺失 | DWD 去重/参数解析失败 | audit 报告标识缺失字段，DWD fallback 到 `endpoint` 正则 |
| location 不匹配 | dataset 建好后 external table 报错 | 建 dataset 前确认 GCS bucket region |
| GCS 自动新增 endpoint（如新指数） | wildcard 静默收入未审计数据 | wildcard 配置记录基线 endpoint 列表；新 endpoint 触发 audit alert，未通过 audit 不入主表 |
| 第 6.5 节 4 对疑似重叠 api 误合 | 同表内字段语义混淆 | 默认全部拆，audit 通过后再考虑合表 |

## 18. 后续问题

1. GCS bucket `data-aquarium` 的实际 region 是什么？影响 `ashare_ods` 的 location 选择。
2. 第 6.5 节 4 对疑似重叠 api 的最终裁定（特别是 `daily_info` vs `sz_daily_info`、`st` vs `stock_st`）。
3. wildcard 表是否需要在配置中固定 endpoint 白名单，以防 GCS 新增未审计 endpoint 时静默收入。
4. ODS dataset 是否启用 `default_table_expiration_ms`，还是保持永久。
5. 采集器目前是否已稳定写入第 9.3 节列出的 7 个审计字段。
