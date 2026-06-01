$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

if (-not (Test-Path $Python)) {
    Write-Host "Python environment not found. Please make sure .venv exists."
    exit 1
}

Set-Location $ProjectRoot

$ArgsForPython = @("tools\debug_kuaishou_deposit_api.py") + $args
& $Python $ArgsForPython
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
