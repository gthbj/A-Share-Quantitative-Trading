# GCS to BigQuery Pipeline

GCS Parquet 到 BigQuery 的装载管道。本管道不再把 GCS Parquet 复制成 BigQuery native ODS 表，而是通过 **BigQuery external table** 挂载 GCS Parquet，实现贴源层的低成本、可追溯架构。

## 数据流

```text
GCS Parquet (gs://data-aquarium/a-share/standardized_parquet/)
  -> ashare.ods_*  (external tables, 直接读取 GCS, 不复制数据)
  -> ashare.dwd_*  (native tables, PRD_12 transform-dwd 生成)
  -> BigQueryDataSource 读取回测
```

- **ODS**：external table，贴源层，查询时实时读 GCS Parquet，性能弱于 native table（可接受成本）。
- **DWD**：native table，回测读取层，稳定性能、去重和强类型（PRD_12 实现）。
- **DWS** — 汇总层（待后续 PRD 启用）
- **ADS** — 应用层（待后续 PRD 启用）
- **控制表**：`ashare.ods_external_manifest`、`ashare.ods_external_errors` 为 native BigQuery 表，用于审计和追踪。

## Setup

Install dependencies:

```powershell
python -m pip install -r gcs_to_bigquery\requirements.txt
```

Use Application Default Credentials:

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project data-aquarium
```

## Commands

### 初始化 ODS 环境

确认 `ashare` dataset 并创建控制表（`ods_external_manifest`、`ods_external_errors`）：

```powershell
python gcs_to_bigquery\pipeline.py init-ods --config gcs_to_bigquery\config.yaml
```

### 扫描 GCS 并生成本地 manifest

```powershell
python gcs_to_bigquery\pipeline.py manifest --config gcs_to_bigquery\config.yaml
```

### 创建 ODS 外部表

根据 `config.yaml` 中的 `ods_external_tables` 配置，创建或更新 `ashare.ods_*` external table：

```powershell
python gcs_to_bigquery\pipeline.py create-ods-external --config gcs_to_bigquery\config.yaml
```

### 审计 ODS 外部表

验证 external table 存在、schema 可读、GCS URI 匹配、sample query 可执行：

```powershell
python gcs_to_bigquery\pipeline.py audit-ods --config gcs_to_bigquery\config.yaml
```

验收条件：
- `bq show --format=prettyjson data-aquarium:ashare.ods_fact_equity_kline_1d` 显示 `externalDataConfiguration`
- `externalDataConfiguration.sourceFormat == "PARQUET"`
- `externalDataConfiguration.sourceUris` 全部指向 `gs://data-aquarium/a-share/standardized_parquet/`
- sample query 至少返回 1 行

### 同步 manifest 到 BigQuery

以 APPEND + batch_id 幂等方式同步本地 manifest 到 `ashare.ods_external_manifest`：

```powershell
python gcs_to_bigquery\pipeline.py sync-manifest --config gcs_to_bigquery\config.yaml
```

### 查看进度

```powershell
python gcs_to_bigquery\pipeline.py progress --config gcs_to_bigquery\config.yaml
```

## 已废弃命令

以下命令保留但标记为 deprecated，后续 PRD_12 完成后将移除：

| 旧命令 | 替代命令 | 说明 |
|--------|----------|------|
| `init` | `init-ods` | 单 dataset `ashare` + 控制表 |
| `load` | `create-ods-external` | 不再做 native load |
| `audit-staging` | `audit-ods` | 审计 external table |
| `merge` | `transform-dwd`（PRD_12） | DWD transform |

## 配置结构

`config.yaml` 核心区块：

- `gcs` — GCS bucket / prefix 配置（输入源）
- `dataset` — 单 BigQuery dataset：`ashare`
- `ods_external_tables` — 每张业务表的 external table 配置（destination_table、source_uris、hive_partitioning）
- `tables` — DWD 表配置（primary_key、partition_field、clustering_fields），供 PRD_12 使用
- `defaults` — 通用默认值，含 `ods.external_table_type: "external"`
