@echo off
REM -- Open Windows Firewall for the CNC File Manager --------------------------
REM Run this ONCE per computer. It allows other devices to reach the File
REM Manager on TCP port 8097 from two places only:
REM   - the local shop network (localsubnet)
REM   - your Tailscale network (100.64.0.0/10) for secure remote access
REM It is NOT exposed to the public internet.
REM
REM Self-elevates: you will get one "Allow changes?" prompt - click Yes.

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo Opening firewall for TCP 8097 (local subnet only)...
netsh advfirewall firewall delete rule name="CNC File Manager (8097)" >nul 2>&1
netsh advfirewall firewall add rule name="CNC File Manager (8097)" dir=in action=allow protocol=TCP localport=8097 remoteip=localsubnet,100.64.0.0/10 profile=any
echo.
echo Done. Other devices on this network can now reach:
echo   http://%COMPUTERNAME%:8097
echo.
pause
