"""One interface over two very different machines.

The web UI shows a single file manager with a machine switcher, but the two
machines behave nothing alike underneath:

  - **Multicam** — a bespoke TCP protocol on an AXYZ A2MC controller. One
    connection allowed at a time, held open with a heartbeat, stateful `cwd`.
    Its `Get Files` reply carries no size or timestamp, and it can set the
    machine's current job ("Load"), which the Woodtron has no equivalent of.
  - **Woodtron** — anonymous FTP on Windows CE. Stateless, reconnects per
    operation, and its `LIST` output *does* carry sizes and timestamps.

Rather than teach the front end about both, each machine implements the same
small surface here and advertises what it can do via `capabilities()`. The UI
renders from that: no "Load" button appears for a machine that can't load.

Adding a third machine means adding a class here — not touching the UI.
"""
from __future__ import annotations

import asyncio
import ftplib
import logging
from typing import Optional, Protocol

from .controller import DEFAULT_DIR, ControllerError, RemoteFile, clamp_path_to_root
from .ftp import FtpClient, normalize_ftp_path

log = logging.getLogger(__name__)


class FolderNotFound(Exception):
    """The browsed folder no longer exists on the machine."""


class MachineBackend(Protocol):
    """What the UI needs from any machine."""

    key: str
    label: str
    path_sep: str

    # Read-only: the Multicam derives its root from an env var at import time
    # and exposes it as a property, while the Woodtron stores a plain string.
    # Declaring it read-only lets both satisfy the protocol.
    @property
    def root(self) -> str: ...

    def capabilities(self) -> dict: ...
    async def status(self) -> dict: ...
    async def list_files(self, path: Optional[str]) -> tuple[list[RemoteFile], str]: ...
    async def upload(self, filename: str, content: bytes, path: str = "") -> bool: ...
    async def download(self, filename: str, path: str = "") -> bytes: ...
    async def delete(self, name: str, path: str = "", is_dir: bool = False) -> bool: ...


class MulticamBackend:
    """The AXYZ A2MC controller, via the existing persistent-socket client.

    Holds no reference to the client: it calls `get_client()` afresh every
    time, because the connection is rebuilt on reconnect and a captured
    reference would go stale the first time the machine was power-cycled.
    """

    key = "multicam"
    label = "Multicam"
    path_sep = "\\"

    def __init__(self, get_client, upload_fn, ready_fn):
        self._get_client = get_client
        self._upload_fn = upload_fn
        self._ready_fn = ready_fn

    @property
    def root(self) -> str:
        return DEFAULT_DIR

    def capabilities(self) -> dict:
        return {
            "folders": True,
            "upload": True,
            "download": True,
            "delete": True,
            # Only the Multicam has the concept of a "current job".
            "load": True,
            # Its Get Files reply carries neither, so the UI hides the columns.
            "sizes": False,
            "timestamps": False,
        }

    async def status(self) -> dict:
        client = self._get_client()
        if client is None or not client.connected:
            return {"online": False, "detail": "Not connected", "busy": False}
        ready, reason = self._ready_fn()
        return {
            "online": bool(getattr(client, "channel_open", True)),
            "detail": reason or f"Connected to {client.ip}",
            "busy": bool(getattr(client, "busy_detail", "")),
            "busy_detail": getattr(client, "busy_detail", ""),
            "ready": ready,
        }

    def _require(self):
        client = self._get_client()
        if client is None or not client.connected:
            raise ControllerError("Not connected to the Multicam controller.")
        return client

    async def list_files(self, path: Optional[str]) -> tuple[list[RemoteFile], str]:
        client = self._require()
        files = await client.list_files(path)
        return files, client.cwd

    async def upload(self, filename: str, content: bytes, path: str = "") -> bool:
        # The shared upload path takes a subfolder *relative* to the jobs root,
        # while the UI browses with absolute controller paths — convert here so
        # "upload into the folder I'm looking at" works.
        subdir = ""
        if path:
            absolute = clamp_path_to_root(path)
            root = self.root.rstrip("\\")
            if absolute.lower() != root.lower():
                subdir = absolute[len(root):].strip("\\")
        return await self._upload_fn(filename, content, subdir)

    async def download(self, filename: str, path: str = "") -> bytes:
        client = self._require()
        if path:
            client.cwd = clamp_path_to_root(path)
        return await client.download_file(filename)

    async def delete(self, name: str, path: str = "", is_dir: bool = False) -> bool:
        client = self._require()
        if path:
            client.cwd = clamp_path_to_root(path)
        return await client.delete_item(name)

    async def load(self, filename: str, path: str = "") -> bool:
        client = self._require()
        if path:
            client.cwd = clamp_path_to_root(path)
        return await client.select_file(filename)

    async def current_job(self) -> str:
        return await self._require().current_job()


class WoodtronBackend:
    """The Woodtron, via its Windows CE FTP server.

    Stateless: every call carries the path it operates on, so two browsers
    looking at different folders can't interfere with each other — something
    the Multicam's single stateful connection genuinely can't offer.
    """

    key = "woodtron"
    label = "Woodtron"
    path_sep = "/"

    def __init__(self, client: FtpClient, root: str = "/", probe_timeout: float = 5.0):
        self._ftp = client
        self.root = normalize_ftp_path(root, "/")
        self._probe_timeout = probe_timeout

    def capabilities(self) -> dict:
        return {
            "folders": True,
            "upload": True,
            "download": True,
            "delete": True,
            # No "current job" concept on this machine — the operator picks the
            # file at the machine itself.
            "load": False,
            # Windows CE has no SIZE/MDTM, but its LIST output carries both.
            "sizes": True,
            "timestamps": True,
        }

    async def status(self) -> dict:
        try:
            await asyncio.to_thread(self._ftp.noop, self._probe_timeout)
        except Exception as e:  # noqa: BLE001 — an offline machine is normal
            from .watchers import brief_error

            return {
                "online": False,
                "detail": f"Not reachable at {self._ftp.host} ({brief_error(e)})",
                "busy": False,
                "ready": False,
            }
        return {
            "online": True,
            "detail": f"Connected to {self._ftp.host}",
            "busy": False,
            "ready": True,
        }

    def _resolve(self, path: Optional[str]) -> str:
        return normalize_ftp_path(path or self.root, self.root)

    async def list_files(self, path: Optional[str]) -> tuple[list[RemoteFile], str]:
        target = self._resolve(path)
        try:
            entries = await asyncio.to_thread(self._ftp.list_dir, target)
        except ftplib.error_perm as e:
            # A 5xx here almost always means the folder is gone — someone
            # deleted it while this browser was sitting on it, or a stale
            # bookmark. Say that, rather than surfacing "550 File unavailable".
            raise FolderNotFound(f"Folder not found on the Woodtron: {target}") from e
        base = target.rstrip("/")
        files = [
            RemoteFile(
                name=e.name,
                size=e.size,
                modified=e.modified,
                path=f"{base}/{e.name}",
                is_dir=e.is_dir,
            )
            for e in entries
        ]
        return files, target

    async def upload(self, filename: str, content: bytes, path: str = "") -> bool:
        await asyncio.to_thread(self._ftp.store, self._resolve(path), filename, content)
        return True

    async def download(self, filename: str, path: str = "") -> bytes:
        return await asyncio.to_thread(self._ftp.retrieve, self._resolve(path), filename)

    async def delete(self, name: str, path: str = "", is_dir: bool = False) -> bool:
        await asyncio.to_thread(self._ftp.delete, self._resolve(path), name, is_dir)
        return True
