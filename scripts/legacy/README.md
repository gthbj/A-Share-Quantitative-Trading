# Legacy GCE Recovery Scripts

This directory keeps ad-hoc scripts used during the 2026-05-23 GCE VM Parquet recovery work.

They are retained only as historical reference and must not be used by the new GCS to BigQuery flow.

Original context:

- VM: `ashare-parquet-worker`
- Project: `data-aquarium`
- Zone: `asia-east2-a`

Hard-coded paths:

- `/mnt/localssd/raw`
- `/mnt/localssd/parquet`
- `/mnt/localssd/work`
- `/mnt/localssd/work/table_manifests`
- `/home/admin/ashare_pipeline`

Scripts:

- `cleanup_invalid_checkpoints.py`: removed `.done` checkpoint files when referenced local Parquet outputs no longer existed.
- `restart_cloud_parquet_build.sh`: killed and restarted the historical cloud Parquet build process on the VM.

Retention:

- Keep until PRD_20260523_11 finishes and BigQuery-backed backtest verification passes.
- Delete only after explicit project owner confirmation.
