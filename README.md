# CNC File Manager

Runs on the workshop PC and does two jobs:

1. **Auto-send.** Watches two Google Drive folders and pushes any new `.nc` file
   to the right machine — `MULTICAM` → the Multicam controller, `WOODTRON` → the
   Woodtron. Drop a file in from anywhere in the world; it's on the machine in
   about twenty seconds, then moves itself into that folder's `Sent` subfolder.
   So the main folders only ever hold work still to go.
   See **[WATCH-FOLDER.md](WATCH-FOLDER.md)**.
2. **A web file manager for both machines.** Browse job folders, upload,
   delete, and (on the Multicam) set the current job — from a browser, on any
   device on the shop network. Tabs at the top of the file list switch between
   the Multicam and the Woodtron, so both are managed in one place. This
   replaces the Windows-only AXYZ A2MC "File Manager" app.

It runs in the background from the moment you log in. There's nothing to start.

---

## Where things are

| | |
|---|---|
| **Web UI** | <http://localhost:8097> on the workshop PC; `http://<pc-name>:8097` from other devices |
| **App code and settings** | this folder (`.env` holds every setting) |
| **Python, logs, state** | `C:\CNCFileManager\` — deliberately outside OneDrive |
| **Multicam controller** | `192.168.0.100` |
| **Woodtron** | `192.168.0.200` (FTP) |

The runtime lives outside OneDrive on purpose. A virtual environment is
thousands of files that OneDrive would sync forever and occasionally lock
mid-write, and the state files are rewritten every few seconds. Keeping them on
the local disk also means the service still starts when OneDrive is signed out
or offline.

## Day-to-day

Double-click any of these:

| | |
|---|---|
| `service-status.bat` | Is it running? Are both machines reachable? What was sent last? **Start here when something seems wrong.** |
| `restart-service.bat` | Restart it — run this after editing `.env` |
| `stop-service.bat` | Stop it (it starts again at the next logon) |
| `start.bat` | Run it in a visible window to watch the log live — for testing only; stop the background service first |

Logs: `C:\CNCFileManager\logs\file-manager.log` (rotates, keeps the last few MB).

## Setting it up on a new PC

1. Install **Python 3.9+** from <https://www.python.org/downloads/> — tick
   *Add python.exe to PATH*.
2. Install **Google Drive for Desktop** and sign in. Set the **File Dump**
   folder to *Available offline* so files download fully instead of staying as
   online-only placeholders.
3. Double-click **`bootstrap.bat`** — builds the Python environment.
4. Open **`.env`** in Notepad and check the two watch-folder paths match this
   PC (see [WATCH-FOLDER.md](WATCH-FOLDER.md) for how to find them).
5. Double-click **`INSTALL-BACKGROUND-SERVICE.bat`** — registers it to start at
   every logon, opens the firewall, and starts it now.

`INSTALL.bat` does all of the above in one go, including installing Python and
Tailscale, if you'd rather not do it step by step.

For access from outside the shop, see [REMOTE-ACCESS.md](REMOTE-ACCESS.md).

## The old Woodtron script

There was an older scheduled task, **"Woodtron Watch Folder"**, running
`D:\Scripts\WatchFolder.ps1` and watching the *local* `D:\Woodtron` folder. It's
redundant now that Woodtron jobs come through Google Drive.

Double-click **`disable-old-woodtron-task.bat`** to retire it (needs admin — one
"Allow changes?" prompt). After that, **`D:\Woodtron` is no longer watched by
anything**; anyone still dropping files there must switch to the Drive
`WOODTRON` folder.

To bring it back:

```bash
schtasks /Change /TN "Woodtron Watch Folder" /ENABLE
```

---

## The file browser

Tabs above the file list switch between the two machines. Each tab shows a dot —
green when the machine is answering, red when it isn't — so you can see one is
off without switching to it. The browser remembers which machine you were last
looking at.

| Feature | Multicam | Woodtron |
|---|---|---|
| **Browse folders** | ✓ | ✓ |
| **Upload** (drag-and-drop `.nc`) | ✓ | ✓ |
| **Delete** (one, or tick several) | ✓ | ✓ |
| **File size and date shown** | — its protocol doesn't report them | ✓ |
| **Load** (set the current job) | ✓ | — no such concept; the operator picks the file at the machine |

Uploads and deletes act on the folder you're currently looking at, on the
machine whose tab is selected.

### How each machine is reached

**Multicam** — an **AXYZ A2MC controller** (AXYZ is part of the Multicam group),
over a raw TCP protocol reverse-engineered from the original Windows app. The
shop's unit reports OS `6.505`, machine ID `AXYZ-0511-1105`. Protocol details and
timing quirks are documented at the top of
[`file_manager/controller.py`](file_manager/controller.py). It allows **one
connection at a time**: if the file list is empty or operations time out, check
that the manufacturer's Windows app isn't open and connected somewhere.

**Woodtron** — anonymous FTP on a **Windows CE 7.0** server. It supports no
`MLSD`, `SIZE` or `MDTM`, so every detail comes from parsing its `LIST` output;
see [`file_manager/ftp.py`](file_manager/ftp.py). Unlike the Multicam it's
stateless, so two people can browse different folders at once without
interfering.

Both machines present the same interface to the UI via
[`file_manager/backends.py`](file_manager/backends.py), and each advertises what
it can do — which is why no Load button appears on the Woodtron. Adding a third
machine means adding a class there, not touching the UI.

### If the controller side stops working

- **Empty or wrong file list** — the jobs root may differ. Set `FM_JOBS_ROOT` in
  `.env` and restart.
- **Timeouts or intermittent failures** — this controller refuses a new
  connection for ~7s after the last one closes, and drops an idle one after
  ~3–8s. The constants that handle this (`CONNECT_TIMEOUT`,
  `HEARTBEAT_INTERVAL`, `RECONNECT_DEADLINE`) are near the top of
  `controller.py`.
- **Anything stranger** — the original approach was capturing what the real
  Windows app sends through a logging TCP proxy. Worth a fresh conversation with
  an AI coding agent, pointing it at this repo.

## Tests

```bash
C:\CNCFileManager\venv\Scripts\python.exe -m pytest tests/ -v
```

81 tests covering the A2MC protocol and path handling, the FTP listing parser
(against real captured output from the Woodtron), the route-level connection and
capability gating, and the folder-watcher send/archive rules — no machine and no
Google Drive needed. Worth running after any change.

Type checking, same environment:

```bash
C:\CNCFileManager\venv\Scripts\python.exe -m pyright
```
