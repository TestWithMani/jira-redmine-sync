$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$runDir = Join-Path $root ".run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$log = Join-Path $runDir "supervise.log"

function Write-Log([string]$message) {
    "$(Get-Date -Format o) $message" | Add-Content -Path $log
}

$python = $null
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $python = $pythonCommand.Source
}
if (-not $python) {
    $python = "C:\Program Files\Python310\python.exe"
}

$listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Log "already listening on 8765"
    exit 0
}

Write-Log "starting python app.py"
Start-Process -FilePath $python -ArgumentList "app.py" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 2
$listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Log "started"
    exit 0
}
Write-Log "failed to listen on 8765"
exit 1
