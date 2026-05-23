# BigQuery 核心层标准化与成本收尾 PRD

## 1. 背景

截至 2026-05-23，本次 GCP 数据迁移已经完成以下阶段：

- 原始非分钟级 A 股数据已在 GCE VM 上转换为 Parquet。
- Parquet 已上传到 GCS：
  - Bucket：`gs://data-aquarium`
  - Prefix：`a-share/standardized_parquet/`
  - 文件数：`13744`
  - 远端 audit：`13744/13744` 通过
- GCS Parquet 已批量装载到 BigQuery staging：
  - Dataset：`ashare_raw`
  - Staging 表：`ashare_raw.stg_*`
  - 表数：`36`
  - Manifest 状态：`13744/13744 loaded`
  - Manifest 控制表：`ashare_raw.gcs_load_manifest`
- Parquet 构建 VM `ashare-parquet-worker` 已停止，状态为 `TERMINATED`。

当前关键限制是：staging 表仍保留源文件字段名和源文件 schema。例如日 K 表包含 `开盘`、`收盘`、`股票代码` 等字段，而策略侧 `data_layer.bigquery_source.BigQueryDataSource` 期望读取 `ashare_core` 中的英文标准字段，如 `open`、`close`、`security_code`、`partition_month`。

因此，下一阶段不能直接把所有 staging 表无脑 merge 到 `ashare_core`。必须先做字段标准化、类型规范、主键去重和核心表验收。

## 2. 目标

本阶段目标是把已落地的 `ashare_raw.stg_*` 数据转换为可被策略和回测稳定使用的 `ashare_core.*` 核心层表，并完成 GCP 成本收尾。

具体目标：

- 为优先表定义明确的 core schema、字段映射、类型转换和主键。
- 从 `ashare_raw.stg_*` 生成 `ashare_core.*` 标准表。
- 让现有 `BigQueryDataSource` 能实际读取核心行情、股票列表、板块成分等表。
- 建立 core 层数据质量审计，避免字段错映、重复行、空关键字段和分区异常。
- 清理或降本处理 GCE VM、Local SSD、临时 staging 数据和不必要的中间资源。
- 把 BigQuery 查询成本控制机制落实到代码、配置和操作流程中。

## 3. 非目标

本阶段不处理：

- 分钟级数据迁移。
- 新增外部数据源。
- 完整因子库和 `ashare_mart` 研究宽表。
- 对所有 36 张表一次性完成深度业务建模。
- 用 BigQuery 直接替代所有本地缓存机制。

## 4. 当前状态盘点

### 4.1 已完成资源

| 资源 | 状态 | 说明 |
| --- | --- | --- |
| GCS Parquet | 已完成 | `gs://data-aquarium/a-share/standardized_parquet/`，`13744` 文件 |
| BigQuery staging | 已完成 | `ashare_raw.stg_*`，36 张表均有行数 |
| Load manifest | 已完成 | 本地 manifest 和 `ashare_raw.gcs_load_manifest` 均记录 loaded 状态 |
| GCE VM | 已停止 | `ashare-parquet-worker` 状态为 `TERMINATED` |
| Loader 代码 | 已入库 | `gcs_to_bigquery/pipeline.py` 支持 table batch load、V2 字段名映射、staging audit |

### 4.2 尚未完成的问题

| 问题 | 影响 |
| --- | --- |
| staging 字段仍是源文件字段 | 策略侧无法稳定读取 |
| `ashare_core` 目标表未生成或未验收 | 回测读取 BigQuery 仍不可用 |
| 中文字段、括号字段已由 BigQuery V2 映射，但缺少业务语义映射 | 无法确认 `open/high/low/close` 等标准字段 |
| 不同来源同类表字段不完全一致 | 需要按表做 coalesce 和优先级 |
| GCE VM 停止时保留 Local SSD | CPU/内存停止计费，但 Local SSD preserved state 和 boot disk 仍可能计费 |
| BigQuery staging 表体量较大 | 长期保留会产生存储费用 |

## 5. 优先级

### P0：成本与资源收尾

必须先完成，避免迁移资源继续产生不必要费用。

需求：

- 确认 `ashare-parquet-worker` 不再需要保留本地 `/mnt/localssd` 数据。
- 若 GCS 和 BigQuery staging 均确认可用，删除 VM，而不是只停止 VM。
- 删除 VM 前记录：
  - VM 名称、机器类型、磁盘信息。
  - GCS audit 结果。
  - BigQuery staging audit 结果。
- 删除 VM 后确认：
  - VM 不存在或处于已删除状态。
  - boot persistent disk 未残留。
  - Local SSD preserved state 不再计费。

验收标准：

- `gcloud compute instances describe ashare-parquet-worker` 返回不存在，或明确确认用户选择继续保留。
- `gcloud compute disks list --filter="name~ashare-parquet-worker"` 无遗留 boot disk，除非用户明确要求保留。
- 文档记录本次删除前后的状态。

### P0：核心行情表标准化

优先支持策略和回测最依赖的表。

第一批 core 表：

- `ashare_core.fact_equity_kline_1d`
- `ashare_core.fact_fund_kline_1d`
- `ashare_core.fact_index_kline_1d`
- `ashare_core.dim_security`
- `ashare_core.fact_board_component_1d`

字段要求：

`fact_equity_kline_1d`

| core 字段 | 类型 | 来源字段候选 |
| --- | --- | --- |
| `date` | DATE | `date` |
| `partition_month` | INT64 | Hive partition |
| `security_code` | STRING | `security_code` |
| `adjust_type` | STRING | 若源表没有，默认 `none` |
| `open` | FLOAT64 | `开盘`、`开盘价_元_` |
| `high` | FLOAT64 | `最高`、`最高价_元_` |
| `low` | FLOAT64 | `最低`、`最低价_元_` |
| `close` | FLOAT64 | `收盘`、`收盘价_元_` |
| `volume` | FLOAT64 | `成交量`、`成交量_手_` |
| `amount` | FLOAT64 | `成交额`、`成交额_千元_` |
| `source_file` | STRING | `source_file` |
| `source_entry` | STRING | `source_entry` |
| `ingested_at` | TIMESTAMP | 当前转换时间 |

`fact_fund_kline_1d` 与 `fact_index_kline_1d` 参照上述字段，但代码字段分别标准化为：

- 基金：`fund_code`
- 指数：`index_code`

`dim_security`

| core 字段 | 类型 | 来源字段候选 |
| --- | --- | --- |
| `security_code` | STRING | `security_code` |
| `security_name` | STRING | `股票名称`、`股票全称` |
| `security_type` | STRING | 固定或映射为 `stock` |
| `exchange` | STRING | 从 `security_code` 后缀或 `交易所代码` 推导 |
| `list_date` | DATE | `上市日期` |
| `delist_date` | DATE | `退市日期` |
| `is_active` | BOOL | `上市状态` 映射 |
| `source_file` | STRING | `source_file` |
| `ingested_at` | TIMESTAMP | 当前转换时间 |

验收标准：

- 每张 core 表创建成功。
- 主键去重后无重复：
  - 股票日 K：`security_code, date, adjust_type`
  - 基金日 K：`fund_code, date, adjust_type`
  - 指数日 K：`index_code, date`
  - 股票维表：`security_code`
- 关键字段非空率达标：
  - `date`、代码字段、`close` 非空率应接近 100%。
- `partition_month` 与 `date` 一致：
  - `partition_month = EXTRACT(YEAR FROM date) * 100 + EXTRACT(MONTH FROM date)`。

### P1：扩展核心事实表

第二批表用于更完整的策略、风险控制和因子研究。

- `fact_adjust_factor`
- `fact_limit_price_1d`
- `fact_suspend_1d`
- `fact_st_status_1d`
- `fact_board_kline_1d`
- `fact_financial_indicator`
- `fact_balance_sheet`
- `fact_income_statement`
- `fact_cash_flow_statement`

验收标准：

- 每张表有字段映射说明。
- 每张表有主键定义。
- 每张表有 row count、date range、null check、duplicate check。
- 财务表要明确报告期字段、公告日期字段和股票代码字段的优先级。

### P1：策略读取联调

目标是让当前代码从 `ashare_core` 读取真实数据。

需求：

- 使用 `BigQueryDataSource.get_stock_list()` 读取 `dim_security`。
- 使用 `BigQueryDataSource.get_bars()` 读取：
  - 一只股票日 K。
  - 一只 ETF 日 K。
  - 一个指数日 K。
- 使用 `get_index_constituents()` 读取板块或指数成分股。
- 跑最小回测用例，确认不会出现字段缺失、类型错误或全表扫描。

验收标准：

- 能返回非空 DataFrame。
- SQL 必须包含 `partition_month IN (...)`。
- 单次 smoke query 的 scanned bytes 可控。
- 至少一个现有策略能完成一次短区间回测。

### P2：BigQuery 成本控制

需求：

- 在查询层增加 dry-run cost check 工具。
- 对大表查询强制分区条件。
- 对没有 `partition_month` 条件的查询给出警告或拒绝执行。
- 给 BigQuery staging/core 表设置合理的保留策略：
  - staging 表可短期保留。
  - core 表长期保留。
  - manifest/errors 长期保留或按月归档。

验收标准：

- 常用读取路径都能打印或记录 estimated bytes processed。
- 回测入口能够限制最大查询字节数。
- 文档写明哪些表可清理、哪些表不可清理。

## 6. 推荐实施顺序

1. 做资源收尾决策：删除 `ashare-parquet-worker` 或明确保留期限。
2. 写 `gcs_to_bigquery/core_transform.py` 或在 `pipeline.py` 中新增 `transform-core` 命令。
3. 先实现 5 张 P0 表的 SQL 转换。
4. 给 P0 表加 `audit-core` 命令。
5. 跑 `BigQueryDataSource` smoke tests。
6. 再扩展 P1 财务、交易约束、板块类表。
7. 最后做 mart 层和策略特征宽表。

## 7. 建议命令接口

新增或扩展：

```bash
python gcs_to_bigquery/pipeline.py transform-core --config gcs_to_bigquery/config.yaml --table fact_equity_kline_1d
python gcs_to_bigquery/pipeline.py transform-core --config gcs_to_bigquery/config.yaml --priority p0
python gcs_to_bigquery/pipeline.py audit-core --config gcs_to_bigquery/config.yaml --table fact_equity_kline_1d
python gcs_to_bigquery/pipeline.py smoke-query --config gcs_to_bigquery/config.yaml
```

行为约定：

- `transform-core` 默认使用 `CREATE OR REPLACE TABLE` 生成 core 表。
- 对事实表按 `partition_month` 分区，按代码字段聚簇。
- 转换 SQL 必须显式选择字段，禁止 `SELECT *` 进入 core。
- 所有转换都写入审计输出，至少包含 row count、date range、duplicate count。

## 8. 数据质量规则

通用规则：

- `target_table` 不进入 core 表，除非作为审计字段。
- `source_file` 和 `source_entry` 保留，便于追溯。
- 所有金额、价格、成交量字段从 STRING 安全转换为 FLOAT64。
- 转换失败的值置为 NULL，并在审计中统计。
- DATE 字段必须统一为 BigQuery DATE 类型。
- `partition_month` 必须为 INT64。

去重规则：

- 同主键多行时，优先保留：
  1. `source_file` 更新日期较新的记录。
  2. 非空字段更多的记录。
  3. 最后按 `source_file, source_entry` 稳定排序。

## 9. 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 中文字段名经 V2 映射后不稳定 | SQL 字段引用失败 | 先用 `client.get_table().schema` 固化实际字段名，再写转换 SQL |
| 不同来源字段单位不同 | 价格/成交额错误 | 字段映射中记录单位，必要时做单位换算 |
| 日 K 表包含不同复权口径 | 回测收益错误 | 明确 `adjust_type` 来源；无复权字段时先标为 `none` |
| staging 表长期保留成本增加 | 持续计费 | 完成 core 验收后按表设置过期或删除 staging |
| 删除 VM 后无法回看本地中间文件 | 排障困难 | 删除前确认 GCS、BigQuery、Git 三处已可复现 |

## 10. 验收清单

- [ ] VM 成本收尾完成，或明确保留理由和截止时间。
- [ ] P0 core 表全部生成。
- [ ] P0 core 表 audit 全部通过。
- [ ] `BigQueryDataSource.get_stock_list()` 返回非空。
- [ ] `BigQueryDataSource.get_bars()` 对股票、基金、指数均返回非空。
- [ ] 一个短周期回测能用 BigQuery 数据源跑通。
- [ ] 查询均包含 `partition_month` 裁剪。
- [ ] BigQuery 查询成本估算可见。
- [ ] staging/core/manifest 的保留策略明确。

## 11. 交付物

- `gcs_to_bigquery` core transform 命令。
- P0 表字段映射配置或 SQL 模板。
- `audit-core` 审计命令。
- smoke test 脚本或测试用例。
- 成本收尾记录。
- 更新后的 README/操作手册。
