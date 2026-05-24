# A Share Data Transfer

Utilities for moving local A-share source files from `D:\A Share` to GCS, building standardized Parquet files, and preparing them for BigQuery loads.

Ignored source directories:

- `D:\A Share\基金_分钟数据`
- `D:\A Share\A股分钟数据`

## Current Layout

Raw source snapshot:

```text
gs://data-aquarium/a-share/raw/source_snapshot=20260523/
```

Standardized Parquet output:

```text
gs://data-aquarium/a-share/standardized_parquet/
```

Cloud worker paths:

```text
/mnt/localssd/raw
/mnt/localssd/parquet
/mnt/localssd/work
```

Incremental raw worker paths for `开盘啦榜单` / `龙虎榜席位` / `资金流向` / selected `指数数据` subsets:

```text
/mnt/localssd/raw_incremental/source_snapshot=20260523
/mnt/localssd/parquet_incremental/source_snapshot=20260523
/mnt/localssd/work/raw_incremental_20260523
```

## Credentials

This project uses Application Default Credentials because service account key creation is disabled by organization policy.

For local upload:

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project data-aquarium
gcloud config set project data-aquarium
```

The authenticated identity needs object access to `gs://data-aquarium`.

If ADC is unavailable but `gcloud auth print-access-token` works, set:

```bash
export ASHARE_USE_GCLOUD_ACCESS_TOKEN=1
```

## Raw Upload

Create or refresh a raw-file manifest:

```powershell
python data_transfer\transfer_raw_to_gcs.py manifest --config data_transfer\config.yaml
```

Upload raw files:

```powershell
python data_transfer\transfer_raw_to_gcs.py upload --config data_transfer\config.yaml
```

Check raw upload progress:

```powershell
python data_transfer\transfer_raw_to_gcs.py progress --config data_transfer\config.yaml
```

## Parquet Build

Local conservative build:

```powershell
python data_transfer\prepare_parquet_to_gcs.py build --config data_transfer\parquet_config.yaml
```

Cloud build on the GCE worker:

```bash
cd /home/admin/ashare_pipeline
nohup .venv/bin/python data_transfer/prepare_parquet_to_gcs.py build \
  --config data_transfer/cloud_parquet_config.yaml \
  > /mnt/localssd/work/build_parquet.log \
  2> /mnt/localssd/work/build_parquet.err.log &
```

Restart cloud build while preserving table-level checkpoints:

```bash
bash scripts/legacy/restart_cloud_parquet_build.sh
```

Remove stale checkpoint files whose Parquet files no longer exist:

```bash
python scripts/legacy/cleanup_invalid_checkpoints.py
```

The two commands above are legacy GCE recovery utilities with hard-coded `/mnt/localssd/...` and `/home/admin/ashare_pipeline` paths. They are kept under `scripts/legacy/` for historical reference only and are not part of the new GCS to BigQuery flow.

Sync incremental raw CSV and selected ZIP files from GCS to the GCE worker after the raw upload is complete:

```bash
cd /home/admin/ashare_pipeline
PYTHONPATH=data_transfer .venv/bin/python data_transfer/prepare_parquet_to_gcs.py sync-raw \
  --config data_transfer/cloud_new_raw_parquet_config.yaml
```

Then build only the incremental Parquet tables:

```bash
PYTHONPATH=data_transfer .venv/bin/python data_transfer/prepare_parquet_to_gcs.py build \
  --config data_transfer/cloud_new_raw_parquet_config.yaml
```

The incremental config intentionally excludes ordinary index OHLCV history such as `指数数据/指数日线行情.zip`,
`指数数据/指数周线行情.zip`, `指数数据/指数月线行情.zip`, and `指数数据/增量数据/指数日线行情/`
because `gs://data-aquarium/a-share/standardized_parquet/` already contains `fact_index_kline_1d`,
`fact_index_kline_1w`, and `fact_index_kline_1mo`.

Audit local Parquet:

```bash
python data_transfer/prepare_parquet_to_gcs.py audit --config data_transfer/cloud_parquet_config.yaml
```

Upload Parquet to GCS:

```bash
python data_transfer/prepare_parquet_to_gcs.py upload --config data_transfer/cloud_parquet_config.yaml
```

Audit remote Parquet:

```bash
python data_transfer/prepare_parquet_to_gcs.py audit --config data_transfer/cloud_parquet_config.yaml --remote
```
