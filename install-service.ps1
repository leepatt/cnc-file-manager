# ── CNC File Manager — install as an always-on background service ───────────
# Registers a scheduled task that starts the File Manager (and both auto-send
# folder watchers) silently at logon and keeps it running.
#
# Run this via INSTALL-BACKGROUND-SERVICE.bat. Safe to run again — it replaces
# the existing task rather than adding a second one.

$ErrorActionPreference = 'Stop'

$TaskName = 'CNC File Manager'
$AppRoot  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Runtime  = 'C:\CNCFileManager'
$Launcher = Join-Path $AppRoot 'run-hidden.vbs'
$Port     = 8097

Write-Host ''
Write-Host '  CNC File Manager — background service setup' -ForegroundColor Cyan
Write-Host '  -------------------------------------------'
Write-Host "  App:     $AppRoot"
Write-Host "  Runtime: $Runtime"
Write-Host ''

# ── 1. Runtime folders ──────────────────────────────────────────────────────
foreach ($dir in @($Runtime, (Join-Path $Runtime 'state'), (Join-Path $Runtime 'logs'))) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}

# ── 2. Sanity checks ────────────────────────────────────────────────────────
$python = Join-Path $Runtime 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "  ERROR: Python environment missing at $python" -ForegroundColor Red
    Write-Host '  Run bootstrap.bat first, then run this again.'
    exit 1
}
if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Host "  ERROR: launcher missing at $Launcher" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $AppRoot '.env'))) {
    Write-Host '  WARNING: no .env found — the watchers will not run.' -ForegroundColor Yellow
}

# ── 3. Keep the app files on this disk ──────────────────────────────────────
# The app lives in a OneDrive folder. If OneDrive frees up space by turning the
# .py files into online-only placeholders, the service fails to start at logon
# (before the network is necessarily up). Pinning the folder prevents that.
try {
    # +P pins, -U clears "online-only". Both are needed: pinning alone does not
    # rehydrate files OneDrive has already dehydrated.
    & attrib.exe +P -U "$AppRoot\*" /s /d 2>&1 | Out-Null
    Write-Host '  Pinned app folder to stay on this disk (OneDrive).' -ForegroundColor Green
} catch {
    Write-Host '  Note: could not pin the app folder for offline use.' -ForegroundColor Yellow
}

# ── 4. Firewall — let other devices on the shop network reach the UI ────────
$ruleName = "CNC File Manager ($Port)"
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    netsh advfirewall firewall delete rule name="$ruleName" | Out-Null
    netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow protocol=TCP `
        localport=$Port remoteip=localsubnet,100.64.0.0/10 profile=any | Out-Null
    Write-Host "  Firewall opened for TCP $Port (local network + Tailscale only)." -ForegroundColor Green
} else {
    Write-Host '  Skipped the firewall rule (needs admin).' -ForegroundColor Yellow
    Write-Host '  Other devices can still be given access later via enable-network.bat.'
}

# ── 5. The scheduled task ───────────────────────────────────────────────────
$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$Launcher`"" -WorkingDirectory $AppRoot

# The trigger's user and the principal's user must be spelled the same way, or
# Register-ScheduledTask rejects the pair with a bare "parameter is incorrect".
$FullUser = "$env:USERDOMAIN\$env:USERNAME"

# One minute after logon: Google Drive needs a moment to mount G:, and the
# watchers report a missing folder until it does. (They recover on their own
# either way — this just avoids a confusing first minute.)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $FullUser
$trigger.Delay = 'PT1M'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $FullUser `
    -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Runs the CNC File Manager and the Multicam + Woodtron auto-send folder watchers.' | Out-Null

Write-Host "  Scheduled task '$TaskName' registered (starts at logon)." -ForegroundColor Green

# ── 6. Start it now ─────────────────────────────────────────────────────────
Start-ScheduledTask -TaskName $TaskName
Write-Host '  Started. Waiting for it to answer...' -NoNewline

# Probe 127.0.0.1 rather than "localhost": the server binds IPv4, and resolving
# localhost can hand back ::1 first and waste the whole timeout on a refused
# IPv6 connection. Check the port first (cheap and unambiguous), then confirm
# the app really answers.
$up = $false
foreach ($i in 1..45) {
    Start-Sleep -Seconds 1
    $sock = New-Object System.Net.Sockets.TcpClient
    try {
        $sock.Connect('127.0.0.1', $Port)
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
        if ($r.status -eq 'ok') { $up = $true }
    } catch {
        Write-Host '.' -NoNewline
    } finally {
        $sock.Close()
    }
    if ($up) { break }
}
Write-Host ''
Write-Host ''

if ($up) {
    Write-Host '  ============================================================' -ForegroundColor Green
    Write-Host '   Running. It will start again automatically at every logon.' -ForegroundColor Green
    Write-Host '  ============================================================' -ForegroundColor Green
    Write-Host ''
    Write-Host "   This computer:  http://localhost:$Port"
    Write-Host "   Other devices:  http://$env:COMPUTERNAME`:$Port"
    Write-Host ''
    Write-Host "   Logs:    $Runtime\logs\file-manager.log"
    Write-Host '   Check:   service-status.bat'
    Write-Host '   Restart: restart-service.bat'
} else {
    Write-Host '  Could not reach the app on port ' -NoNewline -ForegroundColor Red
    Write-Host $Port -ForegroundColor Red
    Write-Host "  Check the log: $Runtime\logs\service.out.log"
}
Write-Host ''
