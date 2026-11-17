@echo off
title CNC File Manager - Stop
REM Stop the background service. The auto-send folders stop too while it is off.
REM It starts again by itself at the next logon, or run restart-service.bat.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service-control.ps1" stop
echo.
pause
