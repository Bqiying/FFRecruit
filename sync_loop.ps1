# ============================================================
# FFRecruit 电脑端自动同步循环脚本
# 每轮: 抓取(--once) -> 导出增量(sync.json) -> scp上传 -> 服务器导入
# 用法:  PowerShell 里运行  .\sync_loop.ps1
# 停止:  Ctrl+C
# ============================================================

$ErrorActionPreference = "Continue"

# ── 配置区（按需修改）──
$ServerHost   = "root@8.138.36.24"     # 服务器 SSH 地址
$RemoteTmp    = "/tmp/sync.json"       # 增量文件上传位置
$RemoteCmd    = "cd /opt/FFRecruit && python3 import_incremental.py $RemoteTmp"  # 服务器导入命令
$LoopSeconds  = 120                    # 每轮间隔（秒），约 2 分钟
$ProjectDir   = "E:\py\ffRecruit"      # 本地项目目录

# ── 主循环 ──
Set-Location $ProjectDir
Write-Host "==== FFRecruit 自动同步已启动 ====" -ForegroundColor Cyan
Write-Host "服务器: $ServerHost | 间隔: ${LoopSeconds}秒 | Ctrl+C 停止"

while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "`n[$ts] === 开始新一轮抓取 ===" -ForegroundColor Green

    # 1. 抓取一轮 + 导出增量
    python scraper.py --once --export sync.json

    # 2. 上传增量 + 服务器导入
    if (Test-Path "$ProjectDir\sync.json") {
        Write-Host "[$ts] 上传增量到服务器..." -ForegroundColor Yellow
        scp -q "$ProjectDir\sync.json" "${ServerHost}:$RemoteTmp"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[$ts] 服务器导入..." -ForegroundColor Yellow
            ssh -o BatchMode=yes $ServerHost $RemoteCmd
        } else {
            Write-Host "[$ts] scp 上传失败，跳过导入" -ForegroundColor Red
        }
    } else {
        Write-Host "[$ts] 本轮未生成增量文件（可能抓取失败），跳过上传" -ForegroundColor Red
    }

    # 3. 等待下一轮
    Write-Host "[$ts] 等待 ${LoopSeconds} 秒后继续..."
    Start-Sleep -Seconds $LoopSeconds
}
