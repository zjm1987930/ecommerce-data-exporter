$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Python not found: $Python"
    Write-Host "Please prepare .venv on the development machine before packaging."
    exit 1
}

Set-Location $ProjectRoot
& $Python (Join-Path $ProjectRoot "tools\build_exe.py")
