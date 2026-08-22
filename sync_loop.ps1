# ============================================================
# FFRecruit auto-sync loop (run on your PC)
# Each round: fetch (--once) -> export incremental (sync.json)
#             -> scp upload -> server import
# Usage:  .\sync_loop.ps1    (stop with Ctrl+C)
# ============================================================

$ErrorActionPreference = "Continue"

# ---- Config (edit as needed) ----
$ServerHost   = "root@8.138.36.24"                                   # server SSH
$RemoteTmp    = "/tmp/sync.json"                                     # remote upload path
$RemoteCmd    = "cd /opt/FFRecruit && python3 import_incremental.py $RemoteTmp"
$LoopSeconds  = 120                                                  # loop interval (s)
$ProjectDir   = "E:\py\ffRecruit"                                    # local project dir

# ---- Main loop ----
Set-Location $ProjectDir
Write-Host "==== FFRecruit auto-sync started ====" -ForegroundColor Cyan
Write-Host "Server: $ServerHost | Interval: ${LoopSeconds}s | Ctrl+C to stop"

while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "`n[$ts] === New round ===" -ForegroundColor Green

    # 1. Fetch one round + export incremental
    python scraper.py --once --export sync.json

    # 2. Upload incremental + import on server
    if (Test-Path "$ProjectDir\sync.json") {
        Write-Host "[$ts] Uploading incremental..." -ForegroundColor Yellow
        scp -q "$ProjectDir\sync.json" "${ServerHost}:$RemoteTmp"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[$ts] Importing on server..." -ForegroundColor Yellow
            ssh -o BatchMode=yes $ServerHost $RemoteCmd
        } else {
            Write-Host "[$ts] scp upload failed, skip import" -ForegroundColor Red
        }
    } else {
        Write-Host "[$ts] No incremental file (fetch may have failed), skip upload" -ForegroundColor Red
    }

    # 3. Wait for next round
    Write-Host "[$ts] Waiting ${LoopSeconds}s..."
    Start-Sleep -Seconds $LoopSeconds
}
