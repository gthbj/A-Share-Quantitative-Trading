# GCS to BigQuery Pipeline

Loads table-organized GCS objects from:

```text
gs://data-aquarium/a-share/standardized_parquet/
```

into BigQuery dataset `ashare` with layer prefixes:

- `ods_*` — 原始层（external table）
- `dwd_*` — 明细层（native 事实表与维表）
- `dws_*` — 汇总层（待后续 PRD 启用）
- `ads_*` — 应用层（待后续 PRD 启用）

ODS uses BigQuery external tables over the existing GCS Parquet files. The pipeline does not create native BigQuery copies for ODS business tables; only manifest and error control tables are native. DWD tables are native and are produced by later transform steps.

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

Create the dataset and ODS native control tables:

```bash
python gcs_to_bigquery/pipeline.py init-ods --config gcs_to_bigquery/config.yaml
```

Build a local GCS load manifest:

```bash
python gcs_to_bigquery/pipeline.py manifest --config gcs_to_bigquery/config.yaml
```

Create or update ODS external tables:

```bash
python gcs_to_bigquery/pipeline.py create-ods-external --config gcs_to_bigquery/config.yaml
```

Audit ODS external tables:

```bash
python gcs_to_bigquery/pipeline.py audit-ods --config gcs_to_bigquery/config.yaml
```

Preview legacy load work. This command is deprecated for the ODS external-table flow and is kept only as a historical compatibility entry.

```bash
python gcs_to_bigquery/pipeline.py load --config gcs_to_bigquery/config.yaml --dry-run
```

Merge one ODS table into DWD after its schema and primary key are confirmed. This command is also deprecated in favor of explicit DWD transform commands.

```bash
python gcs_to_bigquery/pipeline.py merge --config gcs_to_bigquery/config.yaml --table fact_equity_kline_1d
```

Show local manifest progress:

```bash
python gcs_to_bigquery/pipeline.py progress --config gcs_to_bigquery/config.yaml
```

Sync the local manifest into the ODS external manifest control table:

```bash
python gcs_to_bigquery/pipeline.py sync-manifest --config gcs_to_bigquery/config.yaml
```

Legacy staging audit, retained only for old native-load runs:

```bash
python gcs_to_bigquery/pipeline.py audit-staging --config gcs_to_bigquery/config.yaml
```
