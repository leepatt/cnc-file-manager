@echo off
title CNC File Manager
REM -- Run the File Manager in a visible window --------------------------------
REM Normally you do NOT need this: INSTALL-BACKGROUND-SERVICE.bat keeps it
REM running silently at all times. Use this when you want to watch the log live
REM while testing something.
REM
REM If the background service is already running, this will fail with "address
REM already in use" - that is expected. Stop it first with stop-service.bat.
cd /d "%~dp0"

set "PY=C:\CNCFileManager\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo.
  echo   The Python environment is missing.
  echo   Double-click bootstrap.bat first.
  echo.
  pause
  exit /b 1
)

echo.
echo   CNC File Manager
echo   ----------------
echo   This computer:  http://localhost:8097
echo   Other devices:  http://%COMPUTERNAME%:8097
echo   Stop: press Ctrl+C, then close this window
echo.

REM Open the browser a couple of seconds after the server starts.
start "" /b powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://localhost:8097'"

if exist "%~dp0.env" (
  "%PY%" -m uvicorn file_manager.app:app --host 0.0.0.0 --port 8097 --env-file "%~dp0.env"
) else (
  "%PY%" -m uvicorn file_manager.app:app --host 0.0.0.0 --port 8097
)

echo.
echo   Server stopped.
pause
