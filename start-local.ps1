# BerkshireAgent — local dev launcher (no make, no docker)
# Starts backend (uvicorn :8001) and frontend (pnpm dev :3000).
# The Next.js dev server proxies /api/* to the Gateway on 8001, so you only
# need to open http://localhost:3000 in the browser.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host ""
    Write-Host "!! $msg" -ForegroundColor Red
    exit 1
}

Step "Pre-flight: checking required tools"
foreach ($tool in @("uv", "pnpm", "node")) {
    $found = (Get-Command $tool -ErrorAction SilentlyContinue) -ne $null
    if (-not $found) {
        Fail "$tool not found on PATH. Install it first."
    } else {
        Write-Host "  [OK] $tool"
    }
}

Step "Pre-flight: checking secrets in .env"
if (-not (Test-Path ".env")) {
    Fail ".env missing. Copy from .env.example and fill MINIMAX_API_KEY + QDRANT_API_KEY."
}
$envLines = Get-Content ".env"
function Get-Env($key) {
    foreach ($line in $envLines) {
        if ($line -match "^$key=(.+)$") { return $Matches[1].Trim() }
    }
    return $null
}
$minimax = Get-Env "MINIMAX_API_KEY"
$qdrant  = Get-Env "QDRANT_API_KEY"
if (-not $minimax -or $minimax -in @("", "your-minimax-api-key")) {
    Fail "MINIMAX_API_KEY missing or placeholder. Fill it in .env."
}
if (-not $qdrant -or $qdrant -in @("", "your-qdrant-api-key")) {
    Fail "QDRANT_API_KEY missing or placeholder. Fill it in .env."
}
Write-Host "  [OK] .env has MINIMAX_API_KEY (len=$($minimax.Length)) and QDRANT_API_KEY (len=$($qdrant.Length))"

Step "Pre-flight: ensuring config.yaml + extensions_config.json"
if (-not (Test-Path "config.yaml")) {
    Write-Host "  config.yaml missing — copying from config.example.yaml"
    Copy-Item "config.example.yaml" "config.yaml" -Force
}
if (-not (Test-Path "extensions_config.json")) {
    Write-Host "  extensions_config.json missing — copying from extensions_config.example.json"
    Copy-Item "extensions_config.example.json" "extensions_config.json" -Force
}

Step "Pre-flight: ensuring .berkshire-agent home + sandbox dir"
$homeDir = Join-Path $root ".berkshire-agent"
$sandbox = Join-Path $root "backend\sandbox"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

Step "Starting Gateway (uvicorn :8001) in a background terminal..."
$backendCmd = "cd backend; `$env:PYTHONPATH='.'; `$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONUTF8='1'; uv run --locked uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001"
Start-Process powershell -ArgumentList "-NoExit","-Command",$backendCmd -WindowStyle Normal
Write-Host "  Gateway window opened. Wait ~10s for it to print 'Application startup complete.'"

Step "Starting Frontend (Next.js :3000) in another background terminal..."
$frontendCmd = "cd frontend; pnpm dev"
Start-Process powershell -ArgumentList "-NoExit","-Command",$frontendCmd -WindowStyle Normal
Write-Host "  Frontend window opened. Wait ~15s for 'Ready in ...'."

Start-Sleep -Seconds 8

Write-Host ""
Write-Host "Both services launching in their own windows." -ForegroundColor Green
Write-Host ""
Write-Host "  Backend health : http://localhost:8001/health"
Write-Host "  Browser URL    : http://localhost:3000"
Write-Host ""
Write-Host "If the backend health probe fails, check the Gateway terminal for the actual error." -ForegroundColor Yellow
Write-Host ""