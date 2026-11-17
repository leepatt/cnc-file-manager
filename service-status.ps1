# ── CNC File Manager — status check ─────────────────────────────────────────
# The one thing to run when someone says "my file didn't come through".
# Answers, in order: is the service running, can it see the machines, are the
# watched folders visible, and what has it actually sent lately.

$ErrorActionPreference = 'Continue'
$Port    = 8097
$Runtime = 'C:\CNCFileManager'
$Task    = 'CNC File Manager'

function Line($label, $value, $colour = 'Gray') {
    Write-Host ('  {0,-24}  ' -f $label) -NoNewline
    Write-Host $value -ForegroundColor $colour
}

Write-Host ''
Write-Host '  CNC File Manager — status' -ForegroundColor Cyan
Write-Host '  -------------------------'

# ── scheduled task ──────────────────────────────────────────────────────────
$task = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Line 'Autostart task' 'NOT INSTALLED — run INSTALL-BACKGROUND-SERVICE.bat' 'Red'
} else {
    # Pipe the task object rather than passing -TaskName: the name-only form
    # needs a matching -TaskPath and fails with a bare "cannot find the file".
    $info = $task | Get-ScheduledTaskInfo
    Line 'Autostart task' "$($task.State)  (last run $($info.LastRunTime))" 'Green'
}

# ── the app itself ──────────────────────────────────────────────────────────
$health = $null
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
} catch {
    Line 'Web app' "NOT RESPONDING on port $Port" 'Red'
}

if ($null -ne $health) {
    Line 'Web app' "running — http://localhost:$Port" 'Green'

    $c = $health.controller
    if ($c.connected -and $c.link_ok) {
        Line 'Multicam controller' "connected — $($c.name) at $($c.ip)" 'Green'
    } elseif ($c.connected) {
        Line 'Multicam controller' "link down (machine off?) — $($c.ip)" 'Yellow'
    } else {
        Line 'Multicam controller' 'not connected (retrying every 30s)' 'Yellow'
    }

    Write-Host ''
    Write-Host '  Auto-send folders' -ForegroundColor Cyan
    Write-Host '  -----------------'
    if (-not $health.watchers -or $health.watchers.Count -eq 0) {
        Line 'Watchers' 'NONE CONFIGURED — check .env' 'Red'
    }
    foreach ($w in $health.watchers) {
        Write-Host ''
        Write-Host ("  {0}" -f $w.label) -ForegroundColor White
        Line '  folder' $w.directory
        if (-not $w.folder_available) {
            Line '  status' 'FOLDER NOT VISIBLE (is Google Drive running?)' 'Red'
        } elseif ($w.waiting_reason) {
            Line '  status' ("waiting - " + $w.waiting_reason) 'Yellow'
        } else {
            Line '  status' 'watching' 'Green'
        }
        Line '  sent / waiting' "$($w.sent) sent, $($w.pending) waiting, $($w.failed) failed"
        if ($w.last_sent_name) {
            Line '  last sent' "$($w.last_sent_name)  ($($w.last_sent_at))"
        }
        if ($w.last_error) {
            Line '  last error' "$($w.last_error)  ($($w.last_error_at))" 'Yellow'
        }
    }
}

# ── machines on the network ─────────────────────────────────────────────────
Write-Host ''
Write-Host '  Network' -ForegroundColor Cyan
Write-Host '  -------'
foreach ($m in @(
    @{ Name = 'Multicam 192.168.0.100'; Host = '192.168.0.100'; Port = 1028 },
    @{ Name = 'Woodtron 192.168.0.200'; Host = '192.168.0.200'; Port = 21 }
)) {
    $ok = Test-NetConnection -ComputerName $m.Host -Port $m.Port -WarningAction SilentlyContinue -InformationLevel Quiet
    if ($ok) { Line $m.Name 'reachable' 'Green' } else { Line $m.Name 'NOT REACHABLE' 'Red' }
}

Write-Host ''
Write-Host "  Full log: $Runtime\logs\file-manager.log"
Write-Host ''
