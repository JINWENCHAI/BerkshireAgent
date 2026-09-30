# BerkshireAgent — backend-only launcher (no make, no docker)
# For when you only want the Python API, not the Next.js UI.
# Use the embedded Python client (see ../docs/BERKSHIRE_AGENT.md §9) to talk to
# it from a REPL or a script.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".env")) {
    Write-Host "!! .env missing" -ForegroundColor Red; exit 1
}

$homeDir = Join-Path $root ".berkshire-agent"
$sandbox = Join-Path $root "backend\sandbox"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

$env:PYTHONPATH = "."
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Set-Location backend
uv run --locked uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001