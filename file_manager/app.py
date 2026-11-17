"""CNC File Manager — FastAPI service.

Replaces the Windows-only AXYZ A2MC "File Manager" v1.66 app, and folds in the
two auto-send folder watchers so the workshop runs one background process
instead of three.

  - **Multicam** — browse/upload/download/delete/load over the A2MC controller's
    raw TCP protocol (see controller.py), plus a watched Google Drive folder
    whose new `.nc` files are pushed to the controller automatically.
  - **Woodtron** — a watched Google Drive folder whose new `.nc` files are
    pushed over FTP (see watchers.py). This replaces the standalone
    `WatchFolder WOODTRON.ps1` script.

Routes:
  GET  /              — UI
  GET  /health        — health contract (controller + both watchers)
  POST /api/connect   — open connection to a controller IP
  POST /api/disconnect
  GET  /api/status    — connection state + machine info
  GET  /api/watchers  — folder-watcher state and recent activity
  GET  /api/files     — list job files on the controller
  POST /api/upload    — send a .nc file to the controller (optional `dir`
                        form field targets a subfolder of the jobs root,
                        e.g. "WEB-GENERATED/boxmaker")
  GET  /api/download/{filename} — retrieve a .nc file
  POST /api/load/{filename}     — set a .nc file as the controller's current job
  GET  /api/current-job         — the controller's currently-selected job
  DELETE /api/files/{filename}  — delete a file or folder on the controller
  POST /api/search    — scan a subnet for controllers
  GET  /api/machines  — list of recently connected machines (local store)
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from .backends import FolderNotFound, MachineBackend, MulticamBackend, WoodtronBackend
from .controller import (
    DEFAULT_DIR,
    ControllerClient,
    ControllerError,
    resolve_upload_dir,
)
from .ftp import FtpClient
from .watchers import ControllerUploader, FolderWatcher, FtpUploader

FM_DIR = Path(__file__).parent
HUB_URL = os.environ.get("HUB_URL", "")
HOSTNAME = socket.gethostname()
GHOST_CLEANUP_DIR = os.environ.get("FM_GHOST_CLEANUP_DIR", "").strip()
GHOST_CLEANUP_INTERVAL = float(os.environ.get("FM_GHOST_CLEANUP_INTERVAL", "30"))

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    """Read a float setting, falling back to the default on anything unparseable.

    Settings come from a hand-edited `.env`; a stray character in an interval
    must not stop the whole service from starting.
    """
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using %s", name, raw, default)
        return default


# State that must survive restarts (machine list, per-watcher sent-file
# records). Defaults next to the code; point FM_STATE_DIR somewhere off
# OneDrive/Drive for a always-on install so sync never fights the writes.
_STATE_DIR = Path(_env("FM_STATE_DIR") or (FM_DIR / ".."))
_MACHINES_FILE = _STATE_DIR / "machines.json"

# ── logging ──────────────────────────────────────────────────────────────────
# Surface our own INFO logs (connects, watcher uploads) in the console uvicorn
# runs in — uvicorn configures only its own loggers, not ours. When running
# hidden as a background service there is no console, so FM_LOG_DIR adds a
# rotating file that survives restarts and is the first place to look when
# someone says "the file never arrived".
_pkg_log = logging.getLogger("file_manager")
if not _pkg_log.handlers:
    _fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    _stream = logging.StreamHandler()
    _stream.setFormatter(_fmt)
    _pkg_log.addHandler(_stream)
    _pkg_log.setLevel(logging.INFO)
    _pkg_log.propagate = False

    _log_dir = _env("FM_LOG_DIR")
    if _log_dir:
        try:
            Path(_log_dir).mkdir(parents=True, exist_ok=True)
            _file = logging.handlers.RotatingFileHandler(
                Path(_log_dir) / "file-manager.log",
                maxBytes=2_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            _file.setFormatter(_fmt)
            _pkg_log.addHandler(_file)
            # Capture uvicorn's own startup/error lines in the same file, so one
            # log tells the whole story of a failed boot. Attach to "uvicorn"
            # only — "uvicorn.error" propagates up to it, so handling both
            # writes every line twice.
            logging.getLogger("uvicorn").addHandler(_file)
        except OSError as e:  # pragma: no cover — disk/permission problems only
            _pkg_log.warning("Could not open log file in %s: %s", _log_dir, e)

# ── controller connection ────────────────────────────────────────────────────
# One active connection at a time (same model as the original app).
_client: Optional[ControllerClient] = None
_files_cache: Optional[dict] = None
# Serialize uploads (HTTP route + folder watcher) so their cwd swaps can't race.
_upload_lock = asyncio.Lock()
# Set while the app should keep the controller connection alive by itself.
# Cleared when someone clicks Disconnect, so the supervisor doesn't immediately
# undo them; set again on a manual Connect.
_auto_reconnect = False
RECONNECT_INTERVAL = _env_float("FM_RECONNECT_INTERVAL", 30.0)

# ── watcher configuration ────────────────────────────────────────────────────
# Multicam: a local folder (a Google Drive for Desktop folder in the workshop)
# whose new .nc files are pushed to the A2MC controller.
FM_WATCH_DIR = _env("FM_WATCH_DIR")
FM_WATCH_SUBDIR = _env("FM_WATCH_SUBDIR")
FM_WATCH_INTERVAL = _env_float("FM_WATCH_INTERVAL", 10.0)

# Where a delivered file is moved to, as a subfolder of the watched folder.
# Applies to both machines. Set it empty to leave delivered files where they are.
FM_ARCHIVE_SUBFOLDER = os.environ.get("FM_ARCHIVE_SUBFOLDER", "Sent").strip()

# Woodtron: a second folder, pushed over FTP to the Woodtron's own server.
FM_WOODTRON_DIR = _env("FM_WOODTRON_DIR")
# No default host on purpose: the Woodtron tab and watcher appear only when an
# address is configured. Defaulting to this shop's IP would give every other
# install a permanently-offline machine it never asked for.
FM_WOODTRON_HOST = _env("FM_WOODTRON_HOST")
FM_WOODTRON_PORT = int(_env_float("FM_WOODTRON_PORT", 21.0))
FM_WOODTRON_REMOTE_DIR = _env("FM_WOODTRON_REMOTE_DIR", "/")
FM_WOODTRON_USER = _env("FM_WOODTRON_USER", "anonymous")
FM_WOODTRON_PASSWORD = os.environ.get("FM_WOODTRON_PASSWORD", "anonymous@")
FM_WOODTRON_INTERVAL = _env_float("FM_WOODTRON_INTERVAL", 10.0)

_watchers: list[FolderWatcher] = []

# The machines the file browser can switch between. Built once at import so the
# routes and the UI agree on what exists; the Woodtron entry appears whenever an
# FTP host is configured, whether or not its watch folder is set.
_backends: "dict[str, MachineBackend]" = {}


def _woodtron_ftp() -> FtpClient:
    return FtpClient(
        host=FM_WOODTRON_HOST,
        port=FM_WOODTRON_PORT,
        user=FM_WOODTRON_USER,
        password=FM_WOODTRON_PASSWORD,
    )


def _build_backends() -> "dict[str, MachineBackend]":
    built: "dict[str, MachineBackend]" = {}
    multicam = MulticamBackend(
        get_client=lambda: _client,
        upload_fn=_upload_to_controller,
        ready_fn=_controller_ready,
    )
    built[multicam.key] = multicam
    if FM_WOODTRON_HOST:
        woodtron = WoodtronBackend(_woodtron_ftp(), root=FM_WOODTRON_REMOTE_DIR)
        built[woodtron.key] = woodtron
    return built


def _backend(key: str) -> "MachineBackend":
    machine = _backends.get(key)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine: {key}")
    return machine


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Bring up the controller connection, both folder watchers, and the sweeper."""
    global _client, _auto_reconnect, _watchers, _backends
    _backends = _build_backends()
    ip = _env("FM_CONTROLLER_IP")
    name = _env("FM_CONTROLLER_NAME")
    if ip:
        _auto_reconnect = True
        try:
            _client = await _open_controller(ip, name)
            info = _client.info
            _save_machine(_client.ip, info.name or name)
            log.info("Auto-connected to controller %s (%s)", ip, info.name or name)
        except Exception as e:  # noqa: BLE001 — a machine that is off is normal
            log.warning(
                "Auto-connect to %s failed (%s) — retrying every %.0fs",
                ip, e, RECONNECT_INTERVAL,
            )

    _watchers = _build_watchers()
    tasks = [asyncio.create_task(w.run(), name=f"watch:{w.key}") for w in _watchers]
    if ip:
        tasks.append(asyncio.create_task(_reconnect_loop(), name="controller-supervisor"))
    if GHOST_CLEANUP_DIR:
        tasks.append(asyncio.create_task(_ghost_sweep_loop(), name="ghost-sweeper"))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
                pass
        if _client:
            await _client.disconnect()
            _client = None


def _build_watchers() -> list[FolderWatcher]:
    """Create a watcher per configured folder. Unconfigured folders are skipped."""
    built: list[FolderWatcher] = []
    if FM_WATCH_DIR:
        built.append(
            FolderWatcher(
                key="multicam",
                directory=FM_WATCH_DIR,
                uploader=ControllerUploader(
                    send_fn=_upload_to_controller,
                    is_ready=_controller_ready,
                    subdir=FM_WATCH_SUBDIR,
                    label="Multicam",
                    target=_env("FM_CONTROLLER_IP") or "controller",
                ),
                state_path=_STATE_DIR / "watch_state.json",
                interval=FM_WATCH_INTERVAL,
                archive_dir=FM_ARCHIVE_SUBFOLDER,
            )
        )
    else:
        log.info("Multicam watcher off (FM_WATCH_DIR not set)")

    if FM_WOODTRON_DIR and not FM_WOODTRON_HOST:
        log.warning(
            "FM_WOODTRON_DIR is set but FM_WOODTRON_HOST is not — "
            "the Woodtron watcher cannot run without an address."
        )
    if FM_WOODTRON_DIR and FM_WOODTRON_HOST:
        built.append(
            FolderWatcher(
                key="woodtron",
                directory=FM_WOODTRON_DIR,
                uploader=FtpUploader(
                    host=FM_WOODTRON_HOST,
                    port=FM_WOODTRON_PORT,
                    remote_dir=FM_WOODTRON_REMOTE_DIR,
                    user=FM_WOODTRON_USER,
                    password=FM_WOODTRON_PASSWORD,
                    label="Woodtron",
                ),
                state_path=_STATE_DIR / "watch_state_woodtron.json",
                interval=FM_WOODTRON_INTERVAL,
                archive_dir=FM_ARCHIVE_SUBFOLDER,
            )
        )
    else:
        log.info("Woodtron watcher off (FM_WOODTRON_DIR not set)")
    return built


app = FastAPI(title="CNC File Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FM_DIR / "static"), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(str(FM_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
templates = Jinja2Templates(env=_jinja_env)


# ── models ────────────────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    ip: str
    name: str = ""


class SearchRequest(BaseModel):
    ip: str = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_machines() -> list[dict]:
    try:
        return json.loads(_MACHINES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_machine(ip: str, name: str):
    machines = _load_machines()
    # Deduplicate by IP, most-recent first
    machines = [m for m in machines if m.get("ip") != ip]
    machines.insert(0, {"ip": ip, "name": name})
    machines = machines[:10]  # keep last 10, matching original LAST10 registry key
    try:
        _MACHINES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MACHINES_FILE.write_text(json.dumps(machines, indent=2), encoding="utf-8")
    except OSError:
        pass


async def _open_controller(ip: str, name: str = "") -> ControllerClient:
    client = ControllerClient(ip=ip.strip(), name=name.strip())
    await client.connect()
    return client


def _controller_busy() -> bool:
    # "Busy" means a long-running op (e.g. an upload) is holding the data
    # channel — not the sub-second heartbeat, which also briefly takes the lock.
    return _client is not None and bool(_client.busy_detail)


def _controller_ready() -> tuple[bool, str]:
    """Can a watched file be pushed to the controller right now, and if not, why?

    Distinguishes "machine is off" from "an upload is already running" so the
    watcher can report something useful instead of a bare failure — and so it
    doesn't count a powered-down machine as a failed upload attempt.
    """
    if _client is None or not _client.connected:
        target = _env("FM_CONTROLLER_IP") or "the controller"
        return False, f"not connected to {target}"
    if not _client.channel_open:
        return False, f"data channel to {_client.ip} is down (machine off?)"
    if _controller_busy():
        return False, f"controller busy ({_client.busy_detail})"
    return True, ""


def _status_payload() -> dict:
    if not _client or not _client.connected:
        return {"connected": False, "busy": False, "busy_detail": ""}
    return {
        "connected": True,
        "ip": _client.ip,
        "name": _client.info.name or _client.name,
        "machine_id": _client.info.machine_id,
        "serial": _client.info.serial,
        "os_version": _client.info.os_version,
        "permissions": _client.info.permissions,
        "unit_convention": _client.info.unit_convention,
        "busy": _controller_busy(),
        "busy_detail": _client.busy_detail,
        "link_ok": _client.channel_open,
    }


async def _upload_to_controller(filename: str, content: bytes, subdir: str = "") -> bool:
    """Send one .nc file to the controller's jobs root (or a subfolder).

    Shared by the HTTP upload route and the Multicam folder watcher. Serialized
    by `_upload_lock` so two uploads can't clobber each other's `cwd`. Clears
    the file-list cache on success so the UI reflects the new file.
    """
    global _files_cache
    if not _client or not _client.connected:
        raise ControllerError("Not connected to a controller.")
    target_dir = resolve_upload_dir(subdir)
    async with _upload_lock:
        saved_cwd = _client.cwd
        _client.cwd = target_dir
        _client.busy_detail = f"Uploading {filename}"
        try:
            ok = await _client.upload_file(filename, content)
        finally:
            _client.cwd = saved_cwd
            _client.busy_detail = ""
    if ok:
        _files_cache = None
    return ok


async def _reconnect_loop() -> None:
    """Keep the controller connection up for as long as the process runs.

    Without this, a Multicam that happens to be powered off when the PC boots
    leaves the app disconnected forever — the watcher then quietly queues jobs
    that never arrive. Backs off nothing fancy: a fixed retry is fine, the
    machine is either on or it isn't.

    Respects a deliberate Disconnect from the UI (`_auto_reconnect`), so
    clicking Disconnect doesn't fight a reconnect a few seconds later.
    """
    global _client
    ip = _env("FM_CONTROLLER_IP")
    name = _env("FM_CONTROLLER_NAME")
    while True:
        await asyncio.sleep(RECONNECT_INTERVAL)
        if not _auto_reconnect:
            continue
        if _client is not None and _client.connected:
            continue
        try:
            _client = await _open_controller(ip, name)
            _save_machine(_client.ip, _client.info.name or name)
            log.info("Reconnected to controller %s (%s)", ip, _client.info.name or name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the machine is simply off
            log.debug("Controller %s still unreachable (%s)", ip, e)


def _cleanup_ghost_files() -> int:
    """Remove macOS AppleDouble sidecars from the Multicam SMB mount, if configured.

    Runs OFF the upload path (see `_ghost_sweep_loop`) because globbing the SMB
    share can be slow or stall when the Multicam PC is asleep — we never want
    that to block an upload or a UI request.
    """
    if not GHOST_CLEANUP_DIR:
        return 0
    root = Path(GHOST_CLEANUP_DIR)
    if not root.is_dir():
        return 0
    removed = 0
    for ghost in root.glob("._*"):
        try:
            ghost.unlink()
            removed += 1
        except OSError:
            pass
    return removed


async def _ghost_sweep_loop() -> None:
    """Periodically delete macOS `._` sidecars from the Multicam share.

    Decoupled from uploads so a slow/stalled SMB scan can't freeze the app.
    Uploads via the controller protocol can't create ghosts; this guards
    against ghosts arriving by other routes (USB, Finder, Fusion-direct SMB).
    """
    while True:
        await asyncio.sleep(GHOST_CLEANUP_INTERVAL)
        try:
            removed = await asyncio.to_thread(_cleanup_ghost_files)
            if removed:
                log.info("Swept %d ghost file(s) from %s", removed, GHOST_CLEANUP_DIR)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — sweeper must survive transient SMB errors
            log.warning("Ghost sweep failed: %s", e)


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "file-manager",
        "host": HOSTNAME,
        "controller": {
            "connected": _client.connected if _client else False,
            "ip": _client.ip if _client else None,
            "name": _client.name if _client else None,
            "link_ok": _client.channel_open if _client else False,
            "busy": _controller_busy(),
            "busy_detail": _client.busy_detail if _client else "",
        },
        "watchers": [w.status() for w in _watchers],
    }


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_status_payload())


@app.get("/api/watchers")
async def api_watchers() -> JSONResponse:
    """Folder-watcher state for the UI: what's configured, what it has sent,
    and why anything is currently stuck."""
    return JSONResponse({"watchers": [w.status() for w in _watchers]})


@app.post("/api/connect")
async def api_connect(req: ConnectRequest) -> JSONResponse:
    global _client, _auto_reconnect
    if _client and _client.connected:
        await _client.disconnect()

    try:
        _client = await _open_controller(req.ip, req.name)
        info = _client.info
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))

    _auto_reconnect = True
    _save_machine(_client.ip, info.name or req.name)

    return JSONResponse({
        "connected": True,
        "ip": _client.ip,
        "name": info.name or req.name,
        "machine_id": info.machine_id,
        "serial": info.serial,
        "os_version": info.os_version,
        "permissions": info.permissions,
        "unit_convention": info.unit_convention,
    })


@app.post("/api/disconnect")
async def api_disconnect() -> JSONResponse:
    global _client, _auto_reconnect
    # Deliberate disconnect — stop the supervisor putting it straight back.
    _auto_reconnect = False
    if _client:
        await _client.disconnect()
        _client = None
    return JSONResponse({"connected": False})


@app.get("/api/files")
async def api_files(path: Optional[str] = None) -> JSONResponse:
    global _files_cache
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    if _controller_busy() and _files_cache:
        return JSONResponse({
            **_files_cache,
            "busy": True,
            "stale": True,
            "busy_detail": _client.busy_detail or "Controller busy",
        })
    if _controller_busy():
        raise HTTPException(
            status_code=503,
            detail="Controller busy — uploading or syncing. Try again in a moment.",
        )
    try:
        files = await _client.list_files(path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller error: {e}")
    payload = {
        "cwd": _client.cwd,
        "root": DEFAULT_DIR,
        "files": [
            {
                "name": f.name,
                "size": f.size,
                "modified": f.modified,
                "path": f.path,
                "is_dir": f.is_dir,
            }
            for f in files
        ],
        "busy": False,
        "stale": False,
        "busy_detail": "",
    }
    _files_cache = payload
    return JSONResponse(payload)


@app.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...), dir: str = Form("")
) -> JSONResponse:
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    if not file.filename or not file.filename.lower().endswith(".nc"):
        raise HTTPException(status_code=422, detail="Only .nc files are accepted.")
    try:
        target_dir = resolve_upload_dir(dir)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    content = await file.read()
    try:
        ok = await _upload_to_controller(file.filename, content, dir)
    except ControllerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upload failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="Controller rejected the file.")
    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "bytes": len(content),
        "directory": target_dir,
    })


@app.get("/api/download/{filename}")
async def api_download(filename: str) -> Response:
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    try:
        data = await _client.download_file(filename)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Download failed: {e}")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/current-job")
async def api_current_job() -> JSONResponse:
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    try:
        path = await _client.current_job()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read current job: {e}")
    name = path.rstrip("\\").rsplit("\\", 1)[-1] if path else ""
    return JSONResponse({"path": path, "name": name})


@app.post("/api/load/{filename}")
async def api_load(filename: str) -> JSONResponse:
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    try:
        ok = await _client.select_file(filename)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Load failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="Controller rejected the file.")
    return JSONResponse(
        {"ok": True, "filename": filename, "message": f"Current job is now set to {filename}"}
    )


@app.delete("/api/files/{filename}")
async def api_delete(filename: str) -> JSONResponse:
    if not _client or not _client.connected:
        raise HTTPException(status_code=409, detail="Not connected to a controller.")
    try:
        ok = await _client.delete_item(filename)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Delete failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="Controller rejected the delete.")
    return JSONResponse({"ok": True, "filename": filename})


@app.post("/api/search")
async def api_search(req: Optional[SearchRequest] = None) -> JSONResponse:
    """Scan a /24 subnet for controllers.

    Works without an existing connection — that's the point of it, since you
    reach for Search precisely when you don't know the IP. Falls back to the
    connected controller's subnet, then to this PC's own.
    """
    seed = (req.ip if req else "") or (_client.ip if _client else "") or _local_ip()
    if not seed:
        raise HTTPException(
            status_code=422,
            detail="Could not work out which subnet to scan — type an IP first.",
        )
    try:
        found = await ControllerClient(ip=seed).search_controllers()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return JSONResponse({"found": found, "subnet": ".".join(seed.split(".")[:3]) + ".0/24"})


def _local_ip() -> str:
    """This PC's LAN address, via the route the OS would use to leave the host.

    A UDP 'connect' sets the socket's local address without sending anything,
    which beats `gethostbyname(gethostname())` on a multi-homed PC (this one
    has Wi-Fi, Ethernet and two virtual adapters).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


@app.get("/api/machines")
async def api_machines() -> JSONResponse:
    return JSONResponse({"machines": _load_machines()})


# ── machine-agnostic file browser ─────────────────────────────────────────────
# One set of routes for every machine, so the UI switches between them by
# changing a single key rather than knowing anything about TCP vs FTP.

def _file_payload(files, cwd: str, machine: "MachineBackend") -> dict:
    return {
        "machine": machine.key,
        "label": machine.label,
        "cwd": cwd,
        "root": machine.root,
        "sep": machine.path_sep,
        "capabilities": machine.capabilities(),
        "files": [
            {
                "name": f.name,
                "size": f.size,
                "modified": f.modified,
                "path": f.path,
                "is_dir": f.is_dir,
            }
            for f in files
        ],
        "busy": False,
        "stale": False,
        "busy_detail": "",
    }


@app.get("/api/m")
async def api_machine_list() -> JSONResponse:
    """Every machine the browser can show, with what each one can do.

    Status is gathered concurrently — a machine that is powered off costs a
    five-second probe, and doing them in series would make the switcher feel
    broken whenever one machine was asleep.
    """
    keys = list(_backends)
    results = await asyncio.gather(
        *(_backends[k].status() for k in keys), return_exceptions=True
    )
    machines = []
    for key, status in zip(keys, results):
        backend = _backends[key]
        if isinstance(status, BaseException):
            log.warning("Status check for %s failed: %s", key, status)
            status = {"online": False, "detail": "Status check failed", "busy": False}
        machines.append({
            "key": key,
            "label": backend.label,
            "root": backend.root,
            "sep": backend.path_sep,
            "capabilities": backend.capabilities(),
            **status,
        })
    return JSONResponse({"machines": machines})


@app.get("/api/m/{key}/files")
async def api_machine_files(key: str, path: Optional[str] = None) -> JSONResponse:
    global _files_cache
    machine = _backend(key)

    # The Multicam holds one connection shared with uploads and the watcher, so
    # a listing during an upload would block. Serve the last good listing
    # instead, flagged stale, exactly as the single-machine view did.
    if key == "multicam" and _controller_busy():
        if _files_cache:
            return JSONResponse({
                **_files_cache,
                "busy": True,
                "stale": True,
                "busy_detail": _client.busy_detail if _client else "Controller busy",
            })
        raise HTTPException(
            status_code=503,
            detail="Controller busy — uploading or syncing. Try again in a moment.",
        )

    try:
        files, cwd = await machine.list_files(path)
    except FolderNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ControllerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{machine.label} error: {e}")

    payload = _file_payload(files, cwd, machine)
    if key == "multicam":
        _files_cache = payload
    return JSONResponse(payload)


@app.post("/api/m/{key}/upload")
async def api_machine_upload(
    key: str, file: UploadFile = File(...), path: str = Form("")
) -> JSONResponse:
    machine = _backend(key)
    if not file.filename or not file.filename.lower().endswith(".nc"):
        raise HTTPException(status_code=422, detail="Only .nc files are accepted.")
    content = await file.read()
    try:
        ok = await machine.upload(file.filename, content, path)
    except (ControllerError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upload failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"{machine.label} rejected the file.")
    return JSONResponse(
        {"ok": True, "filename": file.filename, "bytes": len(content), "directory": path}
    )


@app.get("/api/m/{key}/download/{filename}")
async def api_machine_download(key: str, filename: str, path: str = "") -> Response:
    machine = _backend(key)
    try:
        data = await machine.download(filename, path)
    except ControllerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Download failed: {e}")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/m/{key}/files/{filename}")
async def api_machine_delete(
    key: str, filename: str, path: str = "", is_dir: bool = False
) -> JSONResponse:
    machine = _backend(key)
    try:
        ok = await machine.delete(filename, path, is_dir)
    except ControllerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Delete failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"{machine.label} rejected the delete.")
    return JSONResponse({"ok": True, "filename": filename})


@app.post("/api/m/{key}/load/{filename}")
async def api_machine_load(key: str, filename: str, path: str = "") -> JSONResponse:
    """Set a file as the machine's current job. Multicam only."""
    machine = _backend(key)
    if not machine.capabilities().get("load"):
        raise HTTPException(
            status_code=409,
            detail=f"{machine.label} has no 'current job' — pick the file at the machine.",
        )
    try:
        ok = await machine.load(filename, path)  # type: ignore[attr-defined]
    except ControllerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Load failed: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"{machine.label} rejected the file.")
    return JSONResponse(
        {"ok": True, "filename": filename, "message": f"Current job is now set to {filename}"}
    )


def _static_version() -> str:
    """A cache-busting token that changes when the CSS or JS changes.

    Browsers on the shop floor sit open for days and happily serve a cached
    app.js, so a UI fix could ship and simply not appear — with no error to
    explain why. Stamping the asset URLs with the newest mtime makes an updated
    file a different URL, so the change lands the moment the page reloads.
    """
    newest = 0.0
    for path in (FM_DIR / "static").rglob("*"):
        if path.suffix in (".js", ".css"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return str(int(newest))


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "hub_url": HUB_URL,
            "hostname": HOSTNAME,
            "static_version": _static_version(),
        },
    )
