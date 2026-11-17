# ── CNC File Manager — background supervisor ────────────────────────────────
# Keeps the File Manager (and with it both auto-send folder watchers) running
# for as long as this PC is logged in. Started automatically by the
# "CNC File Manager" scheduled task; you should not need to run it by hand.
#
# Why a supervisor rather than just launching uvicorn: a scheduled task only
# restarts an action that *fails*. If the server exits cleanly — someone closes
# it, Python hits an unhandled error and exits 0, Windows Update restarts a
# dependency — the task would consider its job done and the watchers would be
# silently dead until somebody noticed a job hadn't been cut. This loop does
# not care why it stopped; it starts it again.

$ErrorActionPreference = 'Continue'

# Where the app lives (this script's own folder) and where its runtime files go.
$AppRoot   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Runtime   = 'C:\CNCFileManager'
$Python    = Join-Path $Runtime 'venv\Scripts\python.exe'
$LogDir    = Join-Path $Runtime 'logs'
$OutLog    = Join-Path $LogDir 'service.out.log'
$EnvFile   = Join-Path $AppRoot '.env'
$Port      = 8097
$RestartDelaySeconds = 5
$MaxRestartDelaySeconds = 300
$MaxOutLogBytes = 5MB

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-ServiceLog($message) {
    $line = "{0}  SERVICE  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -LiteralPath $OutLog -Value $line -Encoding utf8
}

# Keep the raw stdout capture from growing without bound. The app's own log
# (file-manager.log) rotates itself; this file only holds start/stop lines and
# anything Python printed before logging was configured.
if ((Test-Path -LiteralPath $OutLog) -and ((Get-Item -LiteralPath $OutLog).Length -gt $MaxOutLogBytes)) {
    Move-Item -LiteralPath $OutLog -Destination "$OutLog.old" -Force
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-ServiceLog "FATAL: no Python at $Python. Run bootstrap.bat to rebuild it."
    exit 1
}

# Only ever one supervisor. Two would each start a server, the loser would fail
# to bind port 8097, and its supervisor would then respawn it every 5 seconds
# forever - a log full of noise and no clue why. A named mutex is the cheapest
# reliable "am I already running", and it needs no cleanup: Windows releases it
# when the process ends, however it ends.
$mutexName = 'Local\CNCFileManagerSupervisor'
$script:Mutex = New-Object System.Threading.Mutex($false, $mutexName)
$haveLock = $false
try {
    $haveLock = $script:Mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # The previous supervisor was killed rather than stopped. The lock is ours.
    $haveLock = $true
}
if (-not $haveLock) {
    Write-ServiceLog 'Another supervisor is already running - leaving it alone.'
    exit 0
}

$uvicornArgs = @(
    '-m', 'uvicorn', 'file_manager.app:app',
    '--host', '0.0.0.0',
    '--port', "$Port",
    # The web UI polls status every few seconds; access logging would write
    # tens of thousands of useless lines a day and bury the ones that matter.
    '--no-access-log'
)
if (Test-Path -LiteralPath $EnvFile) {
    $uvicornArgs += @('--env-file', $EnvFile)
} else {
    Write-ServiceLog "WARNING: no .env at $EnvFile — the watchers and auto-connect will be off."
}

Set-Location -LiteralPath $AppRoot
Write-ServiceLog "Supervisor started. App root: $AppRoot"

$rapidFailures = 0

while ($true) {
    Write-ServiceLog "Starting File Manager on port $Port"
    $startedAt = Get-Date
    try {
        & $Python $uvicornArgs 2>&1 | ForEach-Object {
            Add-Content -LiteralPath $OutLog -Value $_ -Encoding utf8
        }
        $code = $LASTEXITCODE
    }
    catch {
        $code = -1
        Write-ServiceLog "Launch error: $_"
    }

    # A server that ran for a while and then stopped is a normal restart. One
    # that dies within seconds is a real fault - a bad .env, a port already
    # taken, a broken dependency - and retrying it twice a second would fill
    # the disk with the same error rather than fix it. Back off instead, but
    # never give up: whatever is wrong may well be fixed while we wait.
    $ranFor = (Get-Date) - $startedAt
    if ($ranFor.TotalSeconds -lt 20) { $rapidFailures++ } else { $rapidFailures = 0 }
    $delay = if ($rapidFailures -gt 1) {
        [Math]::Min($RestartDelaySeconds * [Math]::Pow(2, $rapidFailures - 1), $MaxRestartDelaySeconds)
    } else {
        $RestartDelaySeconds
    }

    Write-ServiceLog ("File Manager exited (code {0}) after {1:n0}s. Restarting in {2:n0}s." -f $code, $ranFor.TotalSeconds, $delay)
    if ($rapidFailures -eq 3) {
        Write-ServiceLog 'It keeps failing immediately. Check the lines above, and .env.'
    }
    Start-Sleep -Seconds $delay
}
