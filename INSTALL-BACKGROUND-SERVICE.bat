@echo off
title CNC File Manager - Install background service
REM -- Make the File Manager run in the background, always ---------------------
REM Installs a scheduled task that starts the File Manager silently at logon
REM and keeps it (and both auto-send folder watchers) running.
REM
REM Admin is only used to open the firewall so other devices on the shop
REM network can reach the web UI. Everything else works without it.

cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click Yes^)...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-service.ps1"

echo.
pause
