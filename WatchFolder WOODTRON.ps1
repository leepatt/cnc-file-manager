# ============================================================================
#  SUPERSEDED - kept for reference only. Do not run this.
#
#  The Woodtron auto-send is now part of the CNC File Manager service, which
#  runs in the background at all times. See WATCH-FOLDER.md.
#
#  What replaced it, and why:
#    - It watched the LOCAL folder D:\Woodtron. The team now drops jobs into a
#      Google Drive folder so they can send from anywhere.
#    - It relied on FileSystemWatcher 'Created' events. Google Drive materialises
#      synced files without reliably raising those, so files could arrive and
#      never be noticed. The replacement polls the folder instead.
#    - It uploaded the moment a file appeared, which could push a half-synced
#      file to the machine. The replacement waits for the size to settle.
#    - It had no memory, no retry, and no way to tell whether anything worked.
#
#  There is also a copy of this at D:\Scripts\WatchFolder.ps1, run by the
#  scheduled task "Woodtron Watch Folder". To retire that task:
#      schtasks /Change /TN "Woodtron Watch Folder" /DISABLE
#  Do that only once you're happy the Drive WOODTRON folder is doing the job,
#  and tell anyone still dropping files into D:\Woodtron to switch.
# ============================================================================

# Set the directory to watch and FTP details
$folder = 'D:\Woodtron' # Folder to monitor
$ftpAddress = 'ftp://192.168.0.200/' # FTP server address

# Create a file watcher
$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $folder
$watcher.IncludeSubdirectories = $false
$watcher.EnableRaisingEvents = $true

# Define actions on a file event
$action = {
  param($source, $event)
  # Get the full path of the newly created file
  $fileName = $event.FullPath
  $uploadPath = $ftpAddress + '/' + [System.IO.Path]::GetFileName($fileName)

  # Ensure the file is not hidden
  [System.IO.File]::SetAttributes($fileName, [System.IO.FileAttributes]::Normal)

  # Set up FTP client
  $webClient = New-Object System.Net.WebClient
  $webClient.Credentials = New-Object System.Net.NetworkCredential('anonymous', 'anonymous')
  try {
    $webClient.UploadFile($uploadPath, "STOR", $fileName)
    Write-Host "Uploaded $fileName to $uploadPath"
  }
  catch {
    Write-Error "Failed to upload ${fileName}: $_"
  }
}

# Subscribe to events
Register-ObjectEvent $watcher 'Created' -Action $action
Write-Host 'Watching for changes. This script will run indefinitely until manually stopped.'

# Keep the script running in an infinite loop
while ($true) {
    Start-Sleep -Seconds 10
}
