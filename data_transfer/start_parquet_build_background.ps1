$ErrorActionPreference = "Stop"

$repo = "D:\git\A-Share-Quantitative-Trading"
$stdout = "D:\A_Share_Transfer_Work\build_parquet.log"
$stderr = "D:\A_Share_Transfer_Work\build_parquet.err.log"

Set-Location $repo
New-Item -ItemType Directory -Force -Path "D:\A_Share_Transfer_Work" | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $stdout, $stderr

Start-Process `
  -FilePath "python" `
  -ArgumentList @("data_transfer\prepare_parquet_to_gcs.py", "build", "--config", "data_transfer\parquet_config.yaml") `
  -WorkingDirectory $repo `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden
