@echo off
title Retire the old Woodtron watch-folder task
REM ---------------------------------------------------------------------------
REM Disables the old "Woodtron Watch Folder" scheduled task, which runs
REM D:\Scripts\WatchFolder.ps1 and watches the LOCAL folder D:\Woodtron.
REM
REM The CNC File Manager service now handles Woodtron auto-send from the Google
REM Drive "File Dump\WOODTRON" folder, so this task is redundant.
REM
REM AFTER THIS: files dropped into D:\Woodtron are NO LONGER sent to the
REM machine. Anyone still using that folder must switch to the Drive folder.
REM
REM Needs admin (the old task was created to run with highest privileges),
REM so you will get one "Allow changes?" prompt - click Yes.
REM ---------------------------------------------------------------------------

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click Yes^)...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo   Retiring the old Woodtron watch-folder task
echo   -------------------------------------------
echo.

schtasks /Query /TN "Woodtron Watch Folder" >nul 2>&1
if %errorlevel% neq 0 (
  echo   The task does not exist - nothing to do.
  echo   It has already been removed, or was never on this PC.
  echo.
  pause
  exit /b 0
)

echo   Stopping it if it is running...
schtasks /End /TN "Woodtron Watch Folder" >nul 2>&1

echo   Disabling it...
schtasks /Change /TN "Woodtron Watch Folder" /DISABLE
if %errorlevel% neq 0 (
  echo.
  echo   FAILED to disable it. It is stopped for now, but will start again
  echo   at the next logon. Try again, or disable it in Task Scheduler.
  echo.
  pause
  exit /b 1
)

echo.
echo   Current state:
powershell -NoProfile -Command "$t = Get-ScheduledTask -TaskName 'Woodtron Watch Folder'; Write-Host ('     ' + $t.TaskName + ' -> ' + $t.State)"

echo.
echo   ============================================================
echo    Done. The old task will not start again.
echo.
echo    Woodtron jobs now go through the Google Drive folder:
echo      File Dump\WOODTRON
echo.
echo    D:\Woodtron is no longer watched by anything - tell anyone
echo    still using it to switch to the Drive folder.
echo   ============================================================
echo.
echo   To undo this later:
echo     schtasks /Change /TN "Woodtron Watch Folder" /ENABLE
echo.
pause
