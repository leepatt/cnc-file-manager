@echo off
title CNC File Manager - Installer
REM -- One-click installer. Just double-click this file. -----------------------
REM Sets up everything: Python (if missing), the app, the firewall, and the
REM Tailscale client. You will get ONE "Allow changes?" prompt - click Yes.

REM Re-launch with admin rights if we don't have them yet.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click Yes^)...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"

echo.
pause
