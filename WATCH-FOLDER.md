# Auto-send: drop a .nc in Google Drive, it goes to the machine

Two watched folders, one for each machine. Drop a `.nc` in from anywhere — the
office, home, your phone, someone else's laptop — and it lands on the machine in
the workshop within about 20 seconds.

| Folder (in Google Drive → **File Dump**) | Goes to | How |
|---|---|---|
| `MULTICAM` | Multicam controller, `192.168.0.100` | The controller's own protocol, into its jobs folder |
| `WOODTRON` | Woodtron, `192.168.0.200` | FTP, into the root folder |

An operator still loads and runs the job at the machine as normal. Nothing here
starts a cut.

---

## How it works

Google Drive for Desktop on the workshop PC syncs both folders down to the `G:`
drive. The File Manager service watches them and sends anything new to the right
machine. It runs in the background at all times (see the "Running it" section of
the README), so there is nothing to start.

**It waits for the file to finish syncing.** A file is only sent once its size
has stopped changing, so a half-uploaded job is never pushed to a machine.

**Only `.nc` files** are picked up. Everything else in the folder is ignored.

**No duplicates.** It remembers what it has sent, and that memory survives
restarts and reboots. Re-saving a file with changes sends the new version;
re-saving an identical file does nothing.

**Sent files move to `Sent`.** As soon as a file reaches the machine it is moved
into the `Sent` subfolder of its watch folder. So the main `MULTICAM` and
`WOODTRON` folders only ever hold work that still has to go — if a file is
sitting there, it hasn't been cut yet.

The move only happens after a confirmed delivery, and the file is never removed
from the main folder until it exists in `Sent`. A send that fails leaves the
file exactly where it was, to be retried. If the move itself fails (Drive
briefly locking the file, say) the file stays put and is filed away on a later
pass — it is never lost in between.

Sending a job with a name already in `Sent` doesn't overwrite the old one; it
gets a `(2)` suffix, so `Sent` stays an accurate record of what went to the
machine and when.

**A machine being off is fine.** Files queue up and go as soon as it answers.
Nothing is lost, and nobody has to re-drop anything.

---

## Checking it worked

Three ways, in increasing order of detail:

1. **The web UI** — <http://localhost:8097> on the workshop PC, or
   `http://<that PC's name>:8097` from any device on the shop network. The
   **Auto-send folders** panel on the left shows both machines: whether each
   folder is visible, what was sent last, and anything waiting or failed.
2. **`service-status.bat`** — double-click it for a one-screen summary,
   including whether both machines are reachable on the network.
3. **The log** — `C:\CNCFileManager\logs\file-manager.log`. Every send, every
   failure, with timestamps.

## When a file doesn't arrive

Work down this list:

- **Is the file actually in Drive?** Open the folder on the workshop PC (not
  just on your own machine) and confirm the file is there and fully synced —
  no cloud/pending icon next to it.
- **Is it a `.nc` file?** `.NC` and `.nc` both work; `.txt`, `.tap`, `.gcode`
  and so on are ignored on purpose.
- **Is the machine on?** `service-status.bat` says whether each one answers.
  If it's off, the file is queued and will go by itself once it's on.
- **Is the service running?** `service-status.bat` again. If not,
  `restart-service.bat`.
- **Does the UI show an error for that file?** The activity feed under each
  machine names the file and the reason.

A file the Multicam refuses outright — a name starting with `_`, for instance —
is reported in the UI and *not* retried forever. Rename it and drop it again.

## Changing the settings

Everything lives in `.env` next to this file. Edit it in Notepad, then
double-click `restart-service.bat`.

| Setting | What it does |
|---|---|
| `FM_WATCH_DIR` | The Multicam folder to watch |
| `FM_WATCH_SUBDIR` | Optional subfolder on the controller to drop files into (default: the jobs root) |
| `FM_WATCH_INTERVAL` | Seconds between checks (default 10) |
| `FM_WOODTRON_DIR` | The Woodtron folder to watch |
| `FM_WOODTRON_HOST` | Woodtron FTP address (default `192.168.0.200`) |
| `FM_WOODTRON_REMOTE_DIR` | Where on the Woodtron to put files (default `/`) |
| `FM_WOODTRON_INTERVAL` | Seconds between checks (default 10) |
| `FM_ARCHIVE_SUBFOLDER` | Where delivered files are moved to (default `Sent`). Blank leaves them in place |

Leaving `FM_WATCH_DIR` or `FM_WOODTRON_DIR` blank turns that watcher off.

### The one-off backlog tidy

Files delivered *before* auto-archiving existed were left in the main folders.
To file those away once:

```bash
C:\CNCFileManager\venv\Scripts\python.exe tidy_delivered.py
```

That only reports what it would do. Add `--apply` to actually move them. It
moves a file only when the watcher's record *and* the log agree it was really
delivered, so files merely marked "already sent" during first-run seeding — and
files edited since they were sent — are left alone.

### If a Drive folder gets moved or renamed

The paths in `.env` look alarming:

```
G:\.shortcut-targets-by-id\1zMTCdWG88zuUEqwYxxtSvwDVZi9hLBUq\File Dump\MULTICAM
```

That's just how Google Drive for Desktop exposes a folder that was **shared with
you** rather than owned by you — the long code is the shared folder's ID, and it
stays the same as long as the sharing does. If someone re-shares the folder or
sets it up fresh, that code changes and the path has to be updated: open the
folder in File Explorer via Google Drive, copy the path from the address bar into
`.env`, and run `restart-service.bat`.

The UI will tell you when this has happened — the watcher shows **Folder
missing** rather than **Watching**.

## First run on a new PC

The very first time a watcher runs against a folder, it records everything
already in there as "already sent" instead of uploading it. Otherwise setting up
a new PC would push the folder's entire history at the machine.

If you ever *want* the backlog sent, delete the matching file in
`C:\CNCFileManager\state\` (`watch_state.json` for the Multicam,
`watch_state_woodtron.json` for the Woodtron) and restart — but note that this
re-sends **everything** currently in the folder.

## The old Woodtron script

This replaces `WatchFolder WOODTRON.ps1` (and the copy at
`D:\Scripts\WatchFolder.ps1`), which watched the local `D:\Woodtron` folder and
ran as its own scheduled task.

Retire it by double-clicking **`disable-old-woodtron-task.bat`**. Once you have,
**`D:\Woodtron` is no longer watched by anything** — everything must go through
the Drive `WOODTRON` folder.
