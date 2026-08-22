@echo off
chcp 65001 >nul
title FFRecruit Auto Sync
echo ============================================
echo   FFRecruit Auto Sync - one-click start
echo ============================================
echo.

cd /d E:\py\ffRecruit

REM Check Python exists
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python or fix PATH.
    pause
    exit /b 1
)

REM Check sync script exists
if not exist sync_loop.ps1 (
    echo [ERROR] sync_loop.ps1 not found in E:\py\ffRecruit
    pause
    exit /b 1
)

echo Starting sync loop... (Ctrl+C in the new window to stop)
echo.

REM Allow PowerShell script execution for current user if needed
powershell -NoProfile -Command "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force" >nul 2>&1

REM Launch sync loop in a dedicated window
start "FFRecruit Sync Loop" powershell -NoExit -NoProfile -Command "cd E:\py\ffRecruit; .\sync_loop.ps1"

echo Sync loop launched in a new window.
echo Keep that window open while your PC is on to keep syncing.
echo.
pause
