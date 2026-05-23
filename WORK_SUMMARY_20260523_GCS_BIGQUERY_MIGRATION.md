# 2026-05-23 GCS / BigQuery 数据迁移工作总结

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| 输出时间 | 2026-05-23 23:12:00 |
| 关联分支 | `feature/strategy-expansion` |
| 项目 | `data-aquarium` |
| 主要目标 | 完成 A 股数据从 GCE 本地 Parquet 构建、GCS 上传，到 BigQuery staging 装载的迁移闭环，并补齐后续 core 标准化 PRD 与架构状态 |

## 2. 已完成事项

### 2.1 Parquet 构建与恢复

- 在 GCE VM `ashare-parquet-worker` 上监控并推进 Parquet 构建任务。
- 发现最后一张 `fact_financial_indicator` 表处理明显慢于其他表后，采用更安全的恢复方案：
  - 保留已完成的 35 张表 checkpoint。
  - 只重跑最后一张 `fact_financial_indicator`。
  - 后续重构为 6 个 shard worker 并行处理该表。
- 保持了以下目录不被删除或破坏：
  - `/mnt/localssd/raw`
  - `/mnt/localssd/parquet`
  - `/mnt/localssd/work/table_manifests`
- 将 VM 上修改过的迁移代码同步回本地仓库，并推送到 Git。

### 2.2 GCS Parquet 上传与远端审计

- 完成 Parquet 文件上传到 GCS：
  - GCS 前缀：`gs://data-aquarium/a-share/standardized_parquet/`
  - 文件数：`13744`
  - 远端审计结果：通过
  - 远端数据规模：约 `4.819 GiB`
- 上传逻辑启用了：
  - 上传后校验。
  - 远端 size 一致时跳过重复上传。
  - manifest 驱动的可恢复上传。

### 2.3 BigQuery staging 装载

- 新增并推送 GCS 到 BigQuery 的装载管道：
  - `gcs_to_bigquery/pipeline.py`
  - `gcs_to_bigquery/config.yaml`
- 创建并使用 BigQuery 数据集：
  - `ashare_raw`
  - `ashare_core`
  - `ashare_mart`
- 创建并维护控制表：
  - `ashare_raw.gcs_load_manifest`
  - `ashare_raw.gcs_load_errors`
- 完成 GCS Parquet 到 BigQuery staging 的装载：
  - BigQuery staging 表：`ashare_raw.stg_*`
  - staging 表数：`36`
  - manifest 状态：`13744/13744 loaded`
  - staging audit：通过
- 已确认 staging 中存在大表，例如：
  - `ashare_raw.stg_fact_equity_kline_1d`：约 `73,212,169` 行
  - `ashare_raw.stg_fact_financial_indicator`：约 `341,977` 行

### 2.4 VM 成本收尾

- GCS 上传和 BigQuery staging audit 通过后，已停止 GCE VM：
  - VM：`ashare-parquet-worker`
  - 状态：`TERMINATED`
- 该 VM 停止后不应继续产生 VM compute 运行费用。
- 精确账单金额未写入仓库文档；实际消费金额仍应以 GCP Cloud Billing 为准。

### 2.5 代码与文档入库

- 已把本次迁移相关代码推送到 `origin/feature/strategy-expansion`。
- 已新增或更新迁移相关代码：
  - `data_transfer/`：原始数据到 Parquet / GCS 的迁移工具。
  - `gcs_to_bigquery/pipeline.py`：GCS Parquet 到 BigQuery staging 的装载、审计与 manifest 同步。
  - `gcs_to_bigquery/config.yaml`：GCS、BigQuery dataset、load 策略与表配置。
- 已根据项目偏好补齐后续 PRD：
  - `PRD/PRD_20260523_05_BigQuery核心层标准化与成本收尾.md`
- 已同步更新架构文档：
  - `ARCHITECTURE.md`

## 3. 关键提交记录

| Commit | 内容 |
| --- | --- |
| `773d39a` | 新增可恢复 GCS Parquet 上传管道 |
| `1bccd9f` | 并行化 `fact_financial_indicator` Parquet 构建 |
| `d2c720d` | 新增 GCS Parquet 到 BigQuery staging 的批量装载管道 |
| `c110601` | 初版 BigQuery core 迁移后续需求文档 |
| `09ae287` | 按项目偏好修订 BigQuery core PRD 并同步架构状态 |

## 4. 当前真实状态

| 项目 | 状态 |
| --- | --- |
| GCS Parquet | 已完成，`13744` 个文件 |
| GCS 远端 audit | 已通过 |
| BigQuery staging | 已完成，`ashare_raw.stg_*` 共 36 张表 |
| BigQuery staging audit | 已通过 |
| BigQuery core | 未完成，待按 PRD 生成 `ashare_core` 标准字段表 |
| 默认数据源代码 | 已按 `ashare_core` 目标表设计，但依赖 core 表完成后验收 |
| GCE VM | 已停止，状态 `TERMINATED` |
| 本地 Git 分支 | `feature/strategy-expansion` |
| 远端同步 | 已推送到 `origin/feature/strategy-expansion` |

## 5. 仍未完成的事项

### 5.1 BigQuery core 标准化

当前 `ashare_raw.stg_*` staging 表仍保留源字段 schema。策略侧 `BigQueryDataSource` 期望读取的是 `ashare_core` 中的英文标准字段表。

必须继续完成：

- `ashare_core.fact_equity_kline_1d`
- `ashare_core.fact_fund_kline_1d`
- `ashare_core.fact_index_kline_1d`
- `ashare_core.dim_security`
- `ashare_core.fact_board_component_1d`

### 5.2 字段映射与审计

staging 到 core 不能直接 `SELECT *`，必须显式完成：

- 中文源字段到英文标准字段映射。
- 类型转换。
- 主键去重。
- 分区字段校验。
- 关键字段空值审计。
- `BigQueryDataSource` smoke test。

### 5.3 成本最终确认

VM 已停止，但以下费用仍需以 Cloud Billing 为准确认：

- GCS 对象存储费用。
- BigQuery staging/core 存储费用。
- BigQuery 查询扫描费用。
- 是否存在保留磁盘、快照或其他未释放资源。

## 6. 后续验收标准

- `ashare_core` P0 表全部创建成功，并通过字段、行数、主键、空值、分区一致性审计。
- `BigQueryDataSource.get_bars()` 能从 BigQuery core 表稳定返回框架标准列：
  - `code`
  - `date`
  - `open`
  - `high`
  - `low`
  - `close`
  - `volume`
  - `amount`
- 相同策略、参数、时间区间在同一 BigQuery snapshot 下多次回测结果一致。
- 不修改 `engine/`、`account/`、`strategy/` 中已有交易逻辑。
- 不把未审计通过的 staging 表直接用于回测。
- 成本收尾确认中，GCE VM 不再产生 compute 运行费用。

## 7. 结论

本次迁移已经完成从 GCE 本地 Parquet 构建、GCS 上传、BigQuery staging 装载到 Git 入库的主要闭环。当前阻塞点不在文件上传或 staging 装载，而在 `ashare_core` 标准表尚未生成和验收。下一阶段应严格按 `PRD/PRD_20260523_05_BigQuery核心层标准化与成本收尾.md` 执行 core 标准化和 smoke test，完成后再把 BigQuery 作为真实回测数据源验收。
