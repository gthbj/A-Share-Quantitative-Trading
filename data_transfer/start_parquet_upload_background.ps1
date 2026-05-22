$ErrorActionPreference = "Stop"

$repo = "D:\git\A-Share-Quantitative-Trading"
$stdout = "D:\A_Share_Transfer_Work\upload_parquet_to_gcs.log"
$stderr = "D:\A_Share_Transfer_Work\upload_parquet_to_gcs.err.log"

Set-Location $repo
New-Item -ItemType Directory -Force -Path "D:\A_Share_Transfer_Work" | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $stdout, $stderr

Start-Process `
  -FilePath "python" `
  -ArgumentList @("data_transfer\prepare_parquet_to_gcs.py", "upload", "--config", "data_transfer\parquet_config.yaml") `
  -WorkingDirectory $repo `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden
