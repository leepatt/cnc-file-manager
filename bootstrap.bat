@echo off
title CNC File Manager - Setup
REM -- One-time setup for the CNC File Manager on a Windows PC ------------------
REM Builds the Python environment the app runs on.
REM
REM The environment lives in C:\CNCFileManager\venv, NOT in this folder. This
REM folder is inside OneDrive, and a virtual environment is thousands of files
REM that OneDrive would sync forever and occasionally lock mid-write. Keeping it
REM on the local disk also means the app still starts when OneDrive is offline.
REM
REM Prerequisite: Python 3.9+ installed and on PATH
REM   (https://www.python.org/downloads/ - tick "Add python.exe to PATH").
cd /d "%~dp0"

set "RUNTIME=C:\CNCFileManager"
set "VENV=%RUNTIME%\venv"

where python >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo Python was not found on this PC.
  echo Install Python 3.9 or newer from https://www.python.org/downloads/
  echo During install, TICK "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

echo Creating runtime folders under %RUNTIME% ...
if not exist "%RUNTIME%\state" mkdir "%RUNTIME%\state"
if not exist "%RUNTIME%\logs"  mkdir "%RUNTIME%\logs"

echo Creating virtual environment (%VENV%) ...
python -m venv "%VENV%"
if %errorlevel% neq 0 ( echo Failed to create the virtual environment. & pause & exit /b 1 )

echo Upgrading pip...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip

echo Installing dependencies...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 ( echo Dependency install failed. & pause & exit /b 1 )

echo.
echo ============================================================
echo   Setup complete. Now double-click:
echo.
echo     INSTALL-BACKGROUND-SERVICE.bat
echo.
echo   That makes it start automatically at every logon and keeps
echo   both auto-send folders (Multicam and Woodtron) running.
echo ============================================================
echo.
pause
