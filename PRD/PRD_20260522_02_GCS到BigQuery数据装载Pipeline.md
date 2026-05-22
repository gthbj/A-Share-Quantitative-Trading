# GCS 到 BigQuery 数据装载 Pipeline PRD

## 1. 背景

当前项目已经有 `data_transfer` 目录负责把 `D:\A Share` 中的非分钟级 A 股数据整理并上传到 GCS：

- GCS bucket：`gs://data-aquarium`
- 标准化对象前缀：`a-share/standardized/`
- 对象组织方式：`{target_table}/partition_month={YYYYMM}/{file}.csv`
- 已忽略分钟级目录：`基金_分钟数据`、`A股分钟数据`

下一阶段需要把 GCS 中的标准化对象装载到 BigQuery，形成可查询、可重跑、可审计的数据管道。

## 2. 目标

新增 `gcs_to_bigquery` 模块，负责：

- 从 GCS 扫描 `a-share/standardized/` 下的 CSV/Parquet 对象。
- 生成 GCS 到 BigQuery 装载 manifest。
- 创建 BigQuery dataset 与控制表。
- 把 GCS 对象装载到 `ashare_raw` staging/landing 表。
- 在字段映射和主键确认后，支持从 staging 表 `MERGE` 到 `ashare_core` 目标表。
- 记录每个 GCS 对象的装载状态、批次、错误信息和 BigQuery job id。

第一版默认以 staging-only 为主，不默认写入 `ashare_core`，避免在实际 CSV schema 未完全校验前污染核心表。

## 3. 非目标

第一版不处理：

- 本地 CSV/ZIP/XLSX 解析，这部分仍由 `data_transfer` 负责。
- 分钟级数据。
- 机器学习特征表 `ashare_mart` 的生成。
- 完整中文字段到英文字段的清洗映射。
- 自动修复源文件 schema 问题。

## 4. 数据分层

| 层 | BigQuery dataset | 说明 |
| --- | --- | --- |
| 控制与临时层 | `ashare_raw` | manifest、errors、staging/landing 表、load job 审计 |
| 核心明细层 | `ashare_core` | 清洗、标准化、去重后的事实表和维度表 |
| 研究特征层 | `ashare_mart` | 后续策略、回测、机器学习特征宽表 |

## 5. GCS 输入约定

输入对象来自：

```text
gs://data-aquarium/a-share/standardized/
```

推荐对象路径：

```text
a-share/standardized/{target_table}/partition_month={YYYYMM}/{object_name}.csv
```

示例：

```text
a-share/standardized/fact_equity_kline_1d/partition_month=202505/abc123_daily.csv
```

Pipeline 从路径中解析：

- `target_table`：`fact_equity_kline_1d`
- `partition_month`：`202505`
- 文件格式：按扩展名识别 `CSV` 或 `PARQUET`

无法解析目标表或分区的对象进入 manifest，但状态标为 `invalid`，不参与默认装载。

## 6. BigQuery 目标设计

### 6.1 控制表

`ashare_raw.gcs_load_manifest`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batch_id` | STRING | 批次 ID |
| `gcs_uri` | STRING | GCS 对象 URI |
| `target_table` | STRING | 目标业务表 |
| `partition_month` | INT64 | 分区月份 |
| `object_size` | INT64 | GCS 对象大小 |
| `object_generation` | STRING | GCS generation，用于识别对象版本 |
| `source_format` | STRING | CSV 或 PARQUET |
| `load_mode` | STRING | `staging_only` 或 `merge_to_core` |
| `status` | STRING | `pending`、`loaded`、`merged`、`skipped`、`failed`、`invalid` |
| `bq_job_id` | STRING | BigQuery job id |
| `started_at` | TIMESTAMP | 开始时间 |
| `finished_at` | TIMESTAMP | 结束时间 |
| `error_message` | STRING | 错误信息 |

`ashare_raw.gcs_load_errors`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `occurred_at` | TIMESTAMP | 错误时间 |
| `batch_id` | STRING | 批次 ID |
| `gcs_uri` | STRING | GCS 对象 URI |
| `target_table` | STRING | 目标业务表 |
| `error_type` | STRING | 错误类型 |
| `error_message` | STRING | 错误信息 |

### 6.2 Staging 表

第一版每个目标表对应一个 staging 表：

```text
ashare_raw.stg_{target_table}
```

默认行为：

- CSV 使用 BigQuery autodetect。
- 按批次追加写入 staging 表。
- staging 表额外不强制追加元数据列，避免和源 CSV schema 冲突。
- 元数据通过 `gcs_load_manifest` 关联。

### 6.3 Core 表

当目标表 schema、主键和字段映射确认后，可启用：

```text
load_mode: merge_to_core
```

写入路径：

```text
GCS object -> ashare_raw.stg_{target_table} -> ashare_core.{target_table}
```

`MERGE` 使用配置中的主键字段，例如：

- `fact_equity_kline_1d`：`security_code`, `date`, `adjust_type`
- `fact_fund_kline_1d`：`fund_code`, `date`, `adjust_type`
- `fact_index_kline_1d`：`index_code`, `date`
- `fact_limit_price_1d`：`security_code`, `date`

## 7. 运行流程

### 7.1 初始化

```powershell
python gcs_to_bigquery\pipeline.py init --config gcs_to_bigquery\config.yaml
```

执行内容：

- 创建 `ashare_raw`、`ashare_core`、`ashare_mart` dataset。
- 创建 `gcs_load_manifest` 和 `gcs_load_errors` 控制表。

### 7.2 生成装载 Manifest

```powershell
python gcs_to_bigquery\pipeline.py manifest --config gcs_to_bigquery\config.yaml
```

执行内容：

- 扫描 GCS prefix。
- 解析目标表、分区月份和对象格式。
- 写入本地 JSONL manifest，便于断点续跑和人工审计。

### 7.3 Dry Run

```powershell
python gcs_to_bigquery\pipeline.py load --config gcs_to_bigquery\config.yaml --dry-run
```

执行内容：

- 展示待装载对象数量、大小、目标表分布。
- 不创建 load job。

### 7.4 装载到 Staging

```powershell
python gcs_to_bigquery\pipeline.py load --config gcs_to_bigquery\config.yaml
```

执行内容：

- 按 manifest 顺序装载 `pending` 对象。
- 成功后更新本地 manifest 状态。
- 失败时写入错误信息并停止，后续可从失败对象继续。

### 7.5 可选 Merge 到 Core

当某张表 schema 已确认，并在 config 中启用 `load_mode: merge_to_core` 后：

```powershell
python gcs_to_bigquery\pipeline.py merge --config gcs_to_bigquery\config.yaml --table fact_equity_kline_1d
```

执行内容：

- 确认目标 core 表存在。
- 使用主键从 staging 表 merge 到 core 表。
- 若目标表 schema 不完整，命令失败并给出明确错误。

## 8. 断点续跑

本地 manifest 为 JSONL，默认路径：

```text
D:/A_Share_Transfer_Work/bigquery_load_manifest.jsonl
```

每条记录独立状态更新：

- 成功对象标记为 `loaded`。
- 失败对象标记为 `failed` 并保留错误信息。
- 重新运行时默认跳过 `loaded`、`merged`、`skipped`。

如果 GCS 对象 generation 变化，应视为新版本对象重新装载。

## 9. 质量检查

第一版提供管道级检查：

- 对象路径是否能解析出目标表。
- `partition_month` 是否为合法 `YYYYMM`。
- 文件格式是否为 CSV 或 PARQUET。
- BigQuery load job 是否成功。
- 本地 manifest 状态是否完整。

后续增加数据级检查：

- 目标表主键重复。
- `date` / `security_code` 等核心字段为空。
- 分区行数异常。
- 价格、成交量、涨跌幅异常。
- staging 与 core merge 行数对账。

## 10. 第一阶段交付物

- `gcs_to_bigquery/README.md`
- `gcs_to_bigquery/config.yaml`
- `gcs_to_bigquery/requirements.txt`
- `gcs_to_bigquery/pipeline.py`
- 本 PRD

第一版代码以可运行的 CLI 管道为目标，先完成初始化、manifest、staging load 和可选 merge 框架。
