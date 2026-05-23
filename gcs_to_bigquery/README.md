# GCS to BigQuery Pipeline

Loads table-organized GCS objects from:

```text
gs://data-aquarium/a-share/standardized_parquet/
```

into BigQuery dataset `ashare` with layer prefixes:

- `ods_*` — 原始层（staging / 外部表）
- `dwd_*` — 明细层（core 事实表与维表）
- `dws_*` — 汇总层（待后续 PRD 启用）
- `ads_*` — 应用层（待后续 PRD 启用）

The pipeline defaults to `staging_only` so GCS objects land in `ashare.ods_*` tables before any merge into dwd tables is enabled. Parquet files are loaded in table batches, and BigQuery Hive partitioning reads `partition_month=YYYYMM` from the GCS path into a `partition_month` column.

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

Create datasets and control tables:

```powershell
python gcs_to_bigquery\pipeline.py init --config gcs_to_bigquery\config.yaml
```

Build a local GCS load manifest:

```powershell
python gcs_to_bigquery\pipeline.py manifest --config gcs_to_bigquery\config.yaml
```

Preview load work:

```powershell
python gcs_to_bigquery\pipeline.py load --config gcs_to_bigquery\config.yaml --dry-run
```

Load pending objects into staging tables:

```powershell
python gcs_to_bigquery\pipeline.py load --config gcs_to_bigquery\config.yaml
```

Merge one staging table into core after its schema and primary key are confirmed:

```powershell
python gcs_to_bigquery\pipeline.py merge --config gcs_to_bigquery\config.yaml --table fact_equity_kline_1d
```

Show local manifest progress:

```powershell
python gcs_to_bigquery\pipeline.py progress --config gcs_to_bigquery\config.yaml
```

Sync the local manifest into the BigQuery control table:

```powershell
python gcs_to_bigquery\pipeline.py sync-manifest --config gcs_to_bigquery\config.yaml
```

Verify all loaded staging tables exist and contain rows:

```powershell
python gcs_to_bigquery\pipeline.py audit-staging --config gcs_to_bigquery\config.yaml
```
