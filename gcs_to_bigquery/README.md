# GCS to BigQuery Pipeline

Loads table-organized GCS objects from:

```text
gs://data-aquarium/a-share/standardized_parquet/
```

into BigQuery datasets:

- `ashare_raw`
- `ashare_core`
- `ashare_mart`

The pipeline defaults to `staging_only` so GCS objects land in `ashare_raw.stg_*` tables before any merge into core tables is enabled. Parquet files are loaded in table batches, and BigQuery Hive partitioning reads `partition_month=YYYYMM` from the GCS path into a `partition_month` column.

## Setup

Install dependencies:

```bash
python -m pip install -r gcs_to_bigquery/requirements.txt
```

Default authentication uses Application Default Credentials:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project data-aquarium
```

On GCE, ADC resolves to the VM service account. The fallback `auth.use_gcloud_access_token: true` path is retained for debugging only and refreshes its gcloud token with a subprocess timeout.

The default local manifest is persisted at `${HOME}/.local/state/ashare/ods_pipeline_manifest.jsonl`; it is no longer written under `/tmp`.

## Commands

Create datasets and control tables:

```bash
python gcs_to_bigquery/pipeline.py init --config gcs_to_bigquery/config.yaml
```

Build a local GCS load manifest:

```bash
python gcs_to_bigquery/pipeline.py manifest --config gcs_to_bigquery/config.yaml
```

Preview load work:

```bash
python gcs_to_bigquery/pipeline.py load --config gcs_to_bigquery/config.yaml --dry-run
```

Load pending objects into staging tables. This command is deprecated for the ODS external-table flow and is kept only as a historical compatibility entry until `create-ods-external` is implemented.

```bash
python gcs_to_bigquery/pipeline.py load --config gcs_to_bigquery/config.yaml
```

Merge one staging table into core after its schema and primary key are confirmed:

```bash
python gcs_to_bigquery/pipeline.py merge --config gcs_to_bigquery/config.yaml --table fact_equity_kline_1d
```

Show local manifest progress:

```bash
python gcs_to_bigquery/pipeline.py progress --config gcs_to_bigquery/config.yaml
```

Sync the local manifest into the BigQuery control table:

```bash
python gcs_to_bigquery/pipeline.py sync-manifest --config gcs_to_bigquery/config.yaml
```

Verify all loaded staging tables exist and contain rows:

```bash
python gcs_to_bigquery/pipeline.py audit-staging --config gcs_to_bigquery/config.yaml
```
