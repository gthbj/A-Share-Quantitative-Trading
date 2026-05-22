# A Share Data Transfer

Utilities for moving local A-share source files from `D:\A Share` to Google Cloud Storage.

Raw ZIP files are not uploaded. ZIP files are expanded locally and uploaded as table-organized CSV files to:

```text
gs://data-aquarium/a-share/standardized/
```

Ignored source directories:

- `D:\A Share\基金_分钟数据`
- `D:\A Share\A股分钟数据`

## Credentials

Writing to a private GCS bucket requires an authenticated Google identity with storage write permissions.

Supported credential modes:

- `application_default_credentials`: recommended for this project because service account key creation is disabled by organization policy.
- `service_account_json`: supported by code, but not usable unless the organization policy allows key creation.

The Excel file `D:\GCP_API_KEY.xls` currently contains an API key only. API keys are not sufficient for private GCS object uploads.

For local upload, install Google Cloud CLI and run:

```powershell
gcloud auth application-default login
```

Then make sure your Google user has bucket-level permission on `data-aquarium`:

```text
Storage Object Admin
```

After that, verify permissions:

```powershell
python data_transfer\transfer_raw_to_gcs.py verify --config data_transfer\config.yaml
```

## Commands

Create or refresh a raw-file manifest:

```powershell
python data_transfer\transfer_raw_to_gcs.py manifest --config data_transfer\config.yaml
```

Dry-run upload plan:

```powershell
python data_transfer\transfer_raw_to_gcs.py upload --config data_transfer\config.yaml --dry-run
```

Upload:

```powershell
python data_transfer\transfer_raw_to_gcs.py upload --config data_transfer\config.yaml
```

Check remote upload progress:

```powershell
python data_transfer\transfer_raw_to_gcs.py progress --config data_transfer\config.yaml
```

Start upload in a hidden background process:

```powershell
powershell -ExecutionPolicy Bypass -File data_transfer\start_upload_background.ps1
```

Create or refresh the standardized CSV manifest:

```powershell
python data_transfer\prepare_standardized_to_gcs.py manifest --config data_transfer\standardized_config.yaml
```

Dry-run the standardized upload plan:

```powershell
python data_transfer\prepare_standardized_to_gcs.py upload --config data_transfer\standardized_config.yaml --dry-run
```

Start standardized upload in a hidden background process:

```powershell
powershell -ExecutionPolicy Bypass -File data_transfer\start_standardized_upload_background.ps1
```

Check standardized upload progress:

```powershell
python data_transfer\prepare_standardized_to_gcs.py progress --config data_transfer\standardized_config.yaml
```
