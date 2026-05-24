# BigQuery Internal Pipeline

This package owns BigQuery-internal A-share transforms after ODS external tables already exist.

Boundary:

- `gcs_to_bigquery/`: GCS manifest, ODS external tables, ODS audit.
- `bigquery_pipeline/`: ODS -> DWD, DWD -> DWS, DWS -> ADS, and layer audits.

Common commands:

```bash
python -m bigquery_pipeline.cli transform-dwd --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli audit-dwd --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli transform-dws --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli audit-dws --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli transform-ads --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli audit-ads --config bigquery_pipeline/config.yaml
```

Targeted rebuilds:

```bash
python -m bigquery_pipeline.cli transform-dwd --config bigquery_pipeline/config.yaml --table fact_money_flow_1d
python -m bigquery_pipeline.cli transform-dws --config bigquery_pipeline/config.yaml --table equity_event_money_flow_features_1d
python -m bigquery_pipeline.cli transform-ads --config bigquery_pipeline/config.yaml --table signal_event_money_flow_1d
```

Special repair commands:

```bash
python -m bigquery_pipeline.cli repair-financial-indicator --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli repair-fundamental-inputs --config bigquery_pipeline/config.yaml
```

BigQuery ML baseline:

```bash
python -m bigquery_pipeline.cli train-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli predict-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
python -m bigquery_pipeline.cli audit-bqml-ml-stock-picker --config bigquery_pipeline/config.yaml
```
