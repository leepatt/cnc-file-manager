' -- CNC File Manager - silent launcher -------------------------------------
' Starts run-service.ps1 with no window at all.
'
' A scheduled task set to "run hidden" still flashes a console window at logon
' unless it runs as a different user (which would need a stored password, and
' would lose the G: Google Drive mount, since Drive mounts per user). Launching
' through wscript with intWindowStyle 0 is the way to get a genuinely invisible
' background process while still running as the logged-in user.
'
' bWaitOnReturn is True on purpose. If this script exited straight away, the
' scheduled task would count itself finished the moment it started, so its
' "don't start a second instance" rule would never apply - every restart would
' leave another server running and fighting for port 8097. Waiting here makes
' the task the real owner of the process, so stopping the task really does
' stop the server.

Dim shell, fso, here, ps1
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = here & "\run-service.ps1"

' 0 = hidden window, True = stay alive until the supervisor stops.
shell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """", 0, True
