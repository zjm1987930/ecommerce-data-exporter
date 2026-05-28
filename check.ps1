$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BundledPython = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = $VenvPython

if (-not (Test-Path $Python)) {
    $Python = $BundledPython
}

if (-not (Test-Path $Python)) {
    Write-Host "未找到 Python：$Python"
    exit 1
}

Set-Location $ProjectRoot
& $Python "tools\check_project.py"
