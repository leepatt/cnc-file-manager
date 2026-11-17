#requires -version 5
# CNC File Manager - one-shot installer. Invoked elevated by INSTALL.bat.
# Idempotent: safe to run again if something needs a second go.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }

function Refresh-Path {
  # Pick up PATH changes (e.g. a fresh Python install) without a new window.
  $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
  $user    = [Environment]::GetEnvironmentVariable('Path','User')
  $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Find-Python {
  # Prefer the 'py' launcher, then a real 'python' (ignore the Microsoft Store stub).
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return @{ Exe = $py.Source; Pre = @('-3') } }
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python -and $python.Source -notlike '*\WindowsApps\*') { return @{ Exe = $python.Source; Pre = @() } }
  return $null
}

function Run($exe, [string[]]$argv) {
  & $exe @argv
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $exe $($argv -join ' ')" }
}

Write-Host "CNC File Manager - setup" -ForegroundColor Green
Write-Host "Installs Python (if needed), the app, opens the firewall (shop LAN +"
Write-Host "Tailscale only), and installs the Tailscale client. One-time."

# 1) Python -----------------------------------------------------------------
Write-Step "Checking for Python"
$python = Find-Python
if (-not $python) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Neither Python nor winget is available on this PC." -ForegroundColor Yellow
    Write-Host "Install Python 3 from https://www.python.org/downloads/"
    Write-Host "(tick 'Add python.exe to PATH'), then double-click INSTALL.bat again."
    exit 1
  }
  Write-Host "Not found. Installing Python 3.12 (about a minute)..."
  winget install --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
  Refresh-Path
  $python = Find-Python
  if (-not $python) {
    Write-Host "Python was installed but is not visible in this window yet." -ForegroundColor Yellow
    Write-Host "Restart the PC, then double-click INSTALL.bat again to finish."
    exit 1
  }
}
Write-Host ("Python: " + $python.Exe + " " + ($python.Pre -join ' '))

# 2) Virtual environment ----------------------------------------------------
# Deliberately outside this folder: the app lives in OneDrive, and a virtual
# environment is thousands of files that OneDrive would sync forever and
# occasionally lock mid-write. On the local disk it also survives OneDrive
# being signed out or offline.
Write-Step "Building the app environment (C:\CNCFileManager\venv)"
$runtimeDir = 'C:\CNCFileManager'
$venvDir = Join-Path $runtimeDir 'venv'
$venvPy  = Join-Path $venvDir 'Scripts\python.exe'
foreach ($d in @($runtimeDir, (Join-Path $runtimeDir 'state'), (Join-Path $runtimeDir 'logs'))) {
  New-Item -ItemType Directory -Force $d | Out-Null
}
$venvOk = $false
if (Test-Path -LiteralPath $venvPy) {
  # An environment copied from another PC points at that PC's Python and won't
  # run here — exactly what happened when the old workshop PC was replaced.
  & $venvPy -c "import sys" 2>$null
  if ($LASTEXITCODE -eq 0) { $venvOk = $true }
  else {
    Write-Host "Found an environment built for another PC, rebuilding it for this machine..."
    Remove-Item -LiteralPath $venvDir -Recurse -Force
  }
}
if ($venvOk) {
  Write-Host "Already present and working, reusing it."
} else {
  Run $python.Exe ($python.Pre + @('-m','venv',$venvDir))
}

# 3) Dependencies -----------------------------------------------------------
Write-Step "Installing dependencies"
Run $venvPy @('-m','pip','install','--upgrade','pip','--quiet')
Run $venvPy @('-m','pip','install','-r','requirements.txt','--quiet')
Write-Host "Dependencies installed."

# 4) Firewall ---------------------------------------------------------------
Write-Step "Opening the firewall (shop LAN + Tailscale only, never the public internet)"
Get-NetFirewallRule -DisplayName 'CNC File Manager (8097)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'CNC File Manager (8097)' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8097 -RemoteAddress @('LocalSubnet','100.64.0.0/10') -Profile Any | Out-Null
Write-Host "Firewall rule set."

# 5) Tailscale client -------------------------------------------------------
Write-Step "Installing the Tailscale client (for access from outside the shop)"
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
  Write-Host "Already installed."
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
  try {
    winget install --id Tailscale.Tailscale --silent --accept-package-agreements --accept-source-agreements
    Write-Host "Installed. You will sign in after setup."
  } catch {
    Write-Host "Could not auto-install Tailscale. Get it later from https://tailscale.com/download/windows" -ForegroundColor Yellow
  }
} else {
  Write-Host "winget missing. Install Tailscale later from https://tailscale.com/download/windows" -ForegroundColor Yellow
}

# 6) Always-on background service -------------------------------------------
Write-Step "Setting it up to run in the background at every logon"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install-service.ps1')

# Done ----------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "It is already running, and will start again by itself at every"
Write-Host "logon. To check on it:  service-status.bat"
Write-Host ""
Write-Host "REMOTE ACCESS (one-time): open the Tailscale app, sign in, then see"
Write-Host "REMOTE-ACCESS.md to name this PC and add your laptop and phone."
