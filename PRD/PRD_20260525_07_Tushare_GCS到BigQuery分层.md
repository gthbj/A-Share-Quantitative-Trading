# PRD_20260525_07 Tushare GCS 到 BigQuery 分层

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-25 CST |
| 文档编号 | PRD_20260525_07 |
| 前置 PRD | `PRD_20260525_06_Tushare到GCS原始数据转存链路.md` |
| 需求优先级 | P1 |
| 当前分支 | `codex/tushare-data-source-prd` |

## 2. 目标

在 Tushare 原始数据已经稳定落到 GCS 后，建设从 GCS raw 到 BigQuery 的 ODS/DWD/DWS/ADS 分层链路。

本 PRD 只讨论：

1. 如何读取 `gs://data-aquarium/a-share/tushare/raw/...`。
2. 如何把 raw 转为 BigQuery 可查询的贴源层和标准事实/维表。
3. 如何处理数据质量、主键、分区、聚簇和成本。
4. 如何为后续模型训练提供 PIT 安全的特征宽表。

不讨论 Tushare API 拉取；该部分属于 PRD 06。不讨论具体模型训练；该部分属于 PRD 08。

## 3. 数据入口

输入路径：

```text
gs://data-aquarium/a-share/tushare/raw/
```

输入文件包含：

- Tushare 原始字段。
- `_source`
- `_tushare_api`
- `_endpoint_key`
- `_target_table`
- `_run_id`
- `_ingested_at`
- `_logical_date`
- `_request_params_json`

BigQuery 层不得假设 Tushare API 当前返回值等同于历史快照，必须使用 raw 中保存的实际文件和 ingestion metadata。

## 4. Dataset 规划

建议新建 Tushare 专用 dataset：

```text
data-aquarium.ashare_tushare
```

命名规范：

```text
ods_<table>
dwd_<table>
dws_<domain>_<feature>_<grain>
ads_<signal>_<grain>
audit_<name>
ctl_<name>
```

是否复用现有 `ashare` dataset 由后续实施时决定，但第一版 PRD 建议新 dataset，降低旧数据和 Tushare 新口径混用风险。

## 5. ODS

ODS 是贴源层，只做轻量类型转换和元数据保留，不做业务推导。

核心 ODS 表：

```text
ods_trade_cal
ods_stock_basic
ods_daily
ods_adj_factor
ods_dividend
ods_daily_basic
ods_stk_limit
ods_suspend_d
ods_namechange
ods_moneyflow
ods_margin
ods_margin_detail
ods_income
ods_balancesheet
ods_cashflow
ods_fina_indicator
ods_top10_holders
ods_top10_floatholders
ods_stk_holdernumber
ods_forecast
ods_express
```

要求：

- 保留所有 raw metadata 字段。
- 保留原始 Tushare 日期字符串。
- 增加标准日期列，例如 `date`、`trade_date`、`announcement_date`、`report_period`。
- 所有 ODS 表支持按 `_ingested_at` 或业务日期追踪数据版本。

## 6. DWD

DWD 是标准事实和维表层。

第一批核心 DWD：

```text
dwd_dim_trade_calendar
dwd_dim_security
dwd_dim_security_name_history
dwd_fact_equity_kline_1d
dwd_fact_adjust_factor
dwd_fact_dividend
dwd_fact_equity_daily_basic_1d
dwd_fact_limit_price_1d
dwd_fact_suspend_1d
dwd_fact_money_flow_1d
dwd_fact_margin_1d
dwd_fact_margin_detail_1d
dwd_fact_income_statement
dwd_fact_balance_sheet
dwd_fact_cash_flow_statement
dwd_fact_financial_indicator
dwd_fact_top10_shareholders
dwd_fact_top10_float_shareholders
dwd_fact_shareholder_count
dwd_fact_earnings_forecast
dwd_fact_earnings_express
```

DWD 要求：

- 统一股票代码字段为 `security_code`，保留 Tushare 格式，如 `000001.SZ`。
- 统一交易日期字段为 `date`。
- 统一公告日期为 `announcement_date`。
- 统一报告期为 `report_period`。
- 统一 ingestion 字段为 `source`、`run_id`、`ingested_at`。
- 建立稳定主键，重复主键必须可解释并有去重规则。

## 7. PIT 规则

PIT 是 point-in-time，即“站在当时只能看到当时已经公开的数据”。

关键规则：

| 数据 | 可见性规则 |
|---|---|
| 日线行情 | 用 `trade_date`，如果用于收盘前决策，不能使用当日收盘字段。 |
| 复权因子 | 后续构造复权序列时必须按 as-of，不能直接使用未来回写后的前复权价。 |
| 分红送股 | 使用 `ex_date` 和公告/实施相关字段共同审计。 |
| 财务三表 | 不能按 `period` 直接可见，必须按 `ann_date` / `f_ann_date`。 |
| 财务指标 | 同财务三表，必须按公告日或实际披露日可见。 |
| 业绩预告/快报 | 使用 `ann_date` 或首次公告日。 |
| 股东数据 | 使用公告日；没有公告日时只能标记为弱 PIT，不能进入严格训练集。 |

BigQuery 必须保留：

- `ingested_at`
- `run_id`
- raw GCS URI 或 raw 文件 hash
- 公告日/实际公告日/报告期

否则只能说“算法按 as-of 构造”，不能审计证明当时确实只看到了这些数据。

## 8. DWS 和 ADS

DWS 面向训练特征：

```text
dws_equity_daily_features_tushare_1d
dws_equity_tradability_tushare_1d
dws_equity_market_context_tushare_1d
dws_equity_fundamental_features_tushare_1d
dws_equity_event_money_flow_features_tushare_1d
```

ADS 面向模型和信号：

```text
ads_signal_ml_tushare_v2_1d
```

DWS/ADS 不在第一版强制全部实现，但 DWD 设计必须给这些表留下字段口径。

## 9. 分区和聚簇

建议：

- 日频事实表按 `partition_month` 或 `date` 分区。
- 财务和事件表按 `announcement_date` 分区；如果公告日缺失，按 `report_period` 分区并标记可见性等级。
- 高频或大表按 `security_code` 聚簇。
- 训练 SQL 必须限制日期范围，避免全表扫描。

## 10. 数据质量

每次加载后至少产出 audit：

| 检查 | 要求 |
|---|---|
| 行数 | 与历史基线或 request manifest 对齐。 |
| 主键重复 | 核心事实表不允许无解释重复。 |
| 日期覆盖 | 日频表覆盖所有应有交易日。 |
| 股票覆盖 | 与股票池和上市状态一致。 |
| 空值率 | 核心字段空值率超阈值时报错或告警。 |
| 极值 | OHLC、成交额、换手率、估值等做合理性检查。 |
| PIT 字段 | 财务和事件数据必须有可见日期字段或风险标记。 |

## 11. 验收标准

1. 能从 PRD 06 产出的 raw Parquet 建立 ODS。
2. 能产出至少 p0/p1 核心 DWD 表。
3. `daily + adj_factor + dividend` 能支持后续 as-of 复权构造。
4. 财务和事件表保留公告日、报告期、ingested_at、run_id。
5. 所有大表具备可控分区和聚簇策略。
6. 有 audit 表或 audit 报告，能定位行数、重复、覆盖和空值问题。

## 12. 后续

本 PRD 完成后，进入 PRD 08：基于 BigQuery DWS/ADS 的 Tushare v2 模型训练与回测。
