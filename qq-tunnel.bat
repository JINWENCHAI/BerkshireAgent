@echo off
REM ============================================================
REM QQ Bot Tunnel - Quick public HTTPS for local BerkshireAgent
REM Uses cloudflared quick tunnel (no signup, no domain)
REM ============================================================

set PORT=8001

echo.
echo ============================================================
echo   BerkshireAgent QQ Bot Tunnel
echo ============================================================
echo.
echo   Step 1: Make sure BerkshireAgent is running on port %PORT%
echo   Step 2: Keep this window OPEN while using QQ
echo   Step 3: Copy the https://...trycloudflare.com line below
echo           and add /api/webhooks/qq to it, then paste into
echo           QQ Open Platform's callback URL.
echo.
echo ============================================================
echo.

REM Probe local Gateway health before opening the tunnel.
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://localhost:%PORT%/health' -UseBasicParsing -TimeoutSec 2).StatusCode } catch { Write-Host 'WARNING: localhost:%PORT%/health did not respond. Start BerkshireAgent first (make dev) and try again.'; exit 1 }"
if errorlevel 1 (
    echo.
    echo Please start BerkshireAgent first:
    echo     cd /d C:\Users\13281\Documents\BerkshireAgent
    echo     make dev
    echo.
    pause
    exit /b 1
)

echo.
echo Starting cloudflared quick tunnel on port %PORT% ...
echo Keep this window open. Copy the URL printed below into QQ Open Platform.
echo.

REM Try cloudflared in PATH first; fall back to known install location.
where cloudflared >nul 2>&1
if %ERRORLEVEL% == 0 (
    cloudflared tunnel --url http://localhost:%PORT%
) else if exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
    "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:%PORT%
) else if exist "C:\Program Files\cloudflared\cloudflared.exe" (
    "C:\Program Files\cloudflared\cloudflared.exe" tunnel --url http://localhost:%PORT%
) else (
    echo.
    echo cloudflared not found. Install it with:
    echo     winget install Cloudflare.cloudflared
    echo Then run this script again.
    pause
    exit /b 1
)