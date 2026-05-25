# Tushare -> GCS Cloud Run Job

This job ingests Tushare Pro data directly into GCS under:

```text
gs://data-aquarium/a-share/tushare/
```

It writes append-only Parquet snapshots:

```text
raw/api=<api>/endpoint=<endpoint>/partition_date=<YYYYMMDD>/run_id=<run_id>/*.parquet
_manifests/run_id=<run_id>/
_checkpoints/endpoint=<endpoint>/logical_date=<YYYYMMDD>.json
```

`raw` is enabled by default. `standardized_parquet` is optional and can be enabled in
`config/tushare_to_gcs.yaml` when BigQuery DWD/DWS loading needs a physical standard layer.
P2 financial and earnings endpoints use the 5000-point quarterly VIP interfaces:
`income_vip`, `balancesheet_vip`, `cashflow_vip`, `fina_indicator_vip`,
`forecast_vip`, and `express_vip`.

Tushare client initialization is centralized in `data_ingestion/tushare_client.py`.
The token is read from `TUSHARE_TOKEN`; the HTTP endpoint is read from
`TUSHARE_HTTP_URL` or `config/tushare_to_gcs.yaml`.

Create the token secret once:

```bash
printf '%s' "$TUSHARE_TOKEN" | gcloud secrets create tushare-token \
  --project=data-aquarium \
  --replication-policy=automatic \
  --data-file=-
```

Run the PIT/qfq-risk bucket first:

```bash
PRIORITY=p0 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh
```

Then run the remaining buckets:

```bash
PRIORITY=p1 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh

PRIORITY=p2 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh
```

Run all configured buckets in order:

```bash
PRIORITY= PRIORITY_THROUGH=p2 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh
```

Run a single endpoint smoke test locally:

```bash
ASHARE_USE_GCLOUD_ACCESS_TOKEN=1 TUSHARE_TOKEN=... \
  TUSHARE_HTTP_URL=http://118.89.66.41:8010/ \
  python -m data_ingestion.tushare_to_gcs run \
  --config config/tushare_to_gcs.yaml \
  --endpoint daily \
  --start-date 20260522 \
  --end-date 20260522 \
  --max-calls 1
```
