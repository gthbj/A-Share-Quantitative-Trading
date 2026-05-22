$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\git\A-Share-Quantitative-Trading"
$WorkDir = "D:\A_Share_Transfer_Work"
$LogPath = Join-Path $WorkDir "upload_raw_to_gcs.log"
$ErrPath = Join-Path $WorkDir "upload_raw_to_gcs.err.log"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$Args = @(
    "data_transfer\transfer_raw_to_gcs.py",
    "upload",
    "--config",
    "data_transfer\config.yaml"
)

Start-Process `
    -FilePath "python" `
    -ArgumentList $Args `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $LogPath `
    -RedirectStandardError $ErrPath `
    -WindowStyle Hidden

Write-Host "Started background upload."
Write-Host "Log: $LogPath"
Write-Host "Err: $ErrPath"
