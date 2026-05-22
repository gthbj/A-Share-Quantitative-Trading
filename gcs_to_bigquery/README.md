# GCS to BigQuery Pipeline

Loads table-organized GCS objects from:

```text
gs://data-aquarium/a-share/standardized/
```

into BigQuery datasets:

- `ashare_raw`
- `ashare_core`
- `ashare_mart`

The first implementation defaults to `staging_only` so GCS objects land in `ashare_raw.stg_*` tables before any merge into core tables is enabled.

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
