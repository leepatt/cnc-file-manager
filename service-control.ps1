# ── CNC File Manager — stop / start / restart ───────────────────────────────
# Called by stop-service.bat and restart-service.bat.
#   .\service-control.ps1 stop
#   .\service-control.ps1 restart

param(
    [ValidateSet('stop', 'start', 'restart')]
    [string]$Action = 'restart'
)

$ErrorActionPreference = 'Continue'
$TaskName = 'CNC File Manager'
$Port = 8097

function Stop-App {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    # Kill the supervisor FIRST, then the server it is watching.
    #
    # Order matters, and so does including the supervisor at all. Stopping the
    # task does not reliably take the whole tree with it — it kills the wscript
    # launcher but leaves the supervisor PowerShell orphaned and running. Kill
    # only the server and that orphan simply starts a new one five seconds
    # later, so "stop" stops nothing and "restart" only appears to work.
    #
    # The $PID exclusion is deliberate: never let this sweep match the shell
    # running it.
    $supervisors = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.Name -in 'powershell.exe', 'pwsh.exe', 'wscript.exe' -and
        ($_.CommandLine -like '*run-service.ps1*' -or $_.CommandLine -like '*run-hidden.vbs*')
    }
    foreach ($p in $supervisors) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1

    $servers = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -like '*file_manager.app*'
    }
    foreach ($p in $servers) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }

    # Wait for the listener to actually let go, so a restart doesn't race it
    # and fail to bind.
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 1
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            return
        }
    }
    Write-Host "  Warning: something is still listening on port $Port." -ForegroundColor Yellow
}

function Wait-ForApp {
    foreach ($i in 1..45) {
        Start-Sleep -Seconds 1
        $sock = New-Object System.Net.Sockets.TcpClient
        try {
            # 127.0.0.1, not "localhost": the server binds IPv4 and localhost
            # can resolve to ::1 first, burning the timeout on a refused socket.
            $sock.Connect('127.0.0.1', $Port)
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
            if ($r.status -eq 'ok') { return $r }
        } catch {
            Write-Host '.' -NoNewline
        } finally {
            $sock.Close()
        }
    }
    return $null
}

if ($Action -in 'stop', 'restart') {
    Write-Host '  Stopping...' -ForegroundColor Cyan
    Stop-App
    if ($Action -eq 'stop') {
        Write-Host ''
        Write-Host '  Stopped. Nothing is being auto-sent to either machine.' -ForegroundColor Yellow
        Write-Host '  It will start again by itself at the next logon,'
        Write-Host '  or now if you run restart-service.bat.'
        Write-Host ''
        return
    }
}

Write-Host '  Starting...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$health = Wait-ForApp
Write-Host ''

if ($null -eq $health) {
    Write-Host '  Did not come up.' -ForegroundColor Red
    Write-Host '  Check C:\CNCFileManager\logs\service.out.log'
    Write-Host ''
    return
}

Write-Host "  Running on http://localhost:$Port" -ForegroundColor Green
$c = $health.controller
if ($c.connected -and $c.link_ok) {
    Write-Host "  Multicam: connected to $($c.name) at $($c.ip)" -ForegroundColor Green
} else {
    Write-Host '  Multicam: not connected yet (it keeps retrying)' -ForegroundColor Yellow
}
foreach ($w in $health.watchers) {
    if ($w.folder_available) {
        Write-Host "  $($w.label): watching" -ForegroundColor Green
    } else {
        Write-Host "  $($w.label): folder not visible yet — is Google Drive running?" -ForegroundColor Yellow
    }
}
Write-Host ''
