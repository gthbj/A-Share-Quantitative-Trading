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

## Credentials

This project uses Application Default Credentials because service account key creation is disabled by organization policy.

For local upload:

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project data-aquarium
gcloud config set project data-aquarium
```

The authenticated identity needs object access to `gs://data-aquarium`.

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
bash /home/admin/ashare_pipeline/data_transfer/restart_cloud_parquet_build.sh
```

Remove stale checkpoint files whose Parquet files no longer exist:

```bash
python data_transfer/cleanup_invalid_checkpoints.py
```

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
