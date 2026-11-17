@echo off
title CNC File Manager - Restart
REM Restart the background service. Run this after editing .env.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service-control.ps1" restart
echo.
pause
