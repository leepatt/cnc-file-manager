@echo off
title CNC File Manager - Status
REM Is it running, is it talking to the machines, and are the watchers awake?
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service-status.ps1"
echo.
pause
