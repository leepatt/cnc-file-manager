"""Low-level A2MC controller communication.

Protocol reverse-engineered by packet-capturing the original AXYZ A2MC
"File Manager" v1.66 app through a logging TCP proxy. All traffic is raw
TCP, plain ASCII, '~'-delimited fields, '\\r\\n'-terminated.

Three channels:
  - Port 1028: version handshake. `V` -> `<os_version>` (e.g. "6.600").
  - Port 2000: identity handshake (machine info / id / serial / units).
  - Port 2006: data channel — directory listing, file transfer, delete.

Port 2006 is a **single, persistent** socket, held open for the life of the
connection. This is mandatory, not an optimisation:

  - The controller refuses a *new* 2006 connection for ~7 s after the previous
    one closes (measured live 2026-06-11). Opening a fresh socket per operation
    therefore fails with ECONNREFUSED ~60 % of the time when ops come close
    together (e.g. clicking through folders).
  - An idle 2006 socket is dropped by the controller after ~3–8 s. So we keep it
    alive with a heartbeat: the client sends `heartbeat`, the controller replies
    `handshake`. A 3 s interval holds the socket open indefinitely.

`_data_lock` serialises every use of the socket — data ops and the heartbeat
never touch the stream concurrently, so replies stay aligned. If the socket
drops mid-operation, `_run_data_op` transparently reconnects (respecting the
~7 s cooldown via backoff) and retries the op once.

Commands (port 2006): `Job Filepath`, `Get Files`, `File Open`/`File Read`/
`File Write`/`Clear Status`, `Delete Item`, `Select File` (set current job),
`Current Path` (read current job). Download terminates on a trailing `EOF`
sentinel, not the byte count (see `download_file`).

Controller paths use backslashes and are case-insensitive on the wire,
e.g. `\\hard disk2\\release\\jobs\\JONIK`. File line breaks are carried as
'~' separators in both directions; the app converts '~' <-> newline.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

log = logging.getLogger(__name__)

VERSION_PORT = 1028
INFO_PORT = 2000
DATA_PORT = 2006

APP_VERSION = "1.66"

# Jobs root on the controller. The original AXYZ A2MC unit this was built
# against exposes its job folder as `\hard disk2\release\jobs`. Other
# controllers (including other A2MC/Multicam units) may use a different
# drive letter or path — override with the FM_JOBS_ROOT env var if the
# file list comes back empty or wrong on connect.
DEFAULT_DIR = os.environ.get("FM_JOBS_ROOT", r"\hard disk2\release\jobs").rstrip("\\") or "\\"
FOLDER_TAG = " [folder"


def clamp_path_to_root(path: str, root: str = DEFAULT_DIR) -> str:
    """Restrict navigation to the jobs root and its subfolders."""
    normalized = path.rstrip("\\") or "\\"
    root_norm = root.rstrip("\\") or "\\"
    norm_lower = normalized.lower()
    root_lower = root_norm.lower()
    if norm_lower == root_lower or norm_lower.startswith(root_lower + "\\"):
        return normalized
    return root_norm


def resolve_upload_dir(subdir: str, root: str = DEFAULT_DIR) -> str:
    """Join a caller-supplied subfolder onto the jobs root for uploads.

    `subdir` is relative, `/` or `\\`-separated (e.g. "WEB-GENERATED/boxmaker").
    Empty returns `root` unchanged. Each segment is checked individually —
    `clamp_path_to_root` can't catch `..` here because it does plain
    string-prefix matching, not path normalization, so a segment like `..`
    would still pass its prefix check.
    """
    subdir = subdir.strip().replace("/", "\\").strip("\\")
    if not subdir:
        return root
    for segment in subdir.split("\\"):
        if segment in ("", ".", "..") or ":" in segment:
            raise ValueError(f"Invalid upload directory: {subdir!r}")
    return f"{root.rstrip(chr(92))}\\{subdir}"


CONNECT_TIMEOUT = 5.0
CMD_TIMEOUT = 10.0
TRANSFER_TIMEOUT = 60.0

# Keepalive: client -> "heartbeat", controller -> "handshake". The controller
# drops an idle 2006 socket after ~3-8 s, so beat well inside that window.
HEARTBEAT_INTERVAL = 3.0
HEARTBEAT_CMD = "heartbeat"
# After a 2006 socket closes the controller refuses new connections for ~7 s;
# reconnect attempts back off until this deadline before giving up.
RECONNECT_DEADLINE = 12.0

T = TypeVar("T")


@dataclass
class MachineInfo:
    ip: str
    name: str = ""
    machine_id: str = ""
    serial: str = ""
    os_version: str = ""
    permissions: str = "User"
    unit_convention: str = "metric"


@dataclass
class RemoteFile:
    name: str
    size: int = 0
    modified: str = ""
    path: str = ""
    is_dir: bool = False


class ControllerError(Exception):
    pass


class ControllerClient:
    """Async client for one A2MC controller.

    `connect()` runs the version + identity handshakes, opens the persistent
    2006 data socket, and starts the heartbeat. Every data operation reuses that
    socket, serialized through `_data_lock`. `cwd` tracks the current controller
    directory.
    """

    def __init__(self, ip: str, name: str = ""):
        self.ip = ip
        self.name = name
        self.info = MachineInfo(ip=ip, name=name)
        self.cwd = DEFAULT_DIR
        self.connected = False
        # The controller permits only one data channel (port 2006), so every
        # data operation and the heartbeat are serialized through this lock.
        self._data_lock = asyncio.Lock()
        self.busy_detail: str = ""
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._heartbeat_task: Optional["asyncio.Task[None]"] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def channel_open(self) -> bool:
        """Is the 2006 data channel actually usable right now?

        `connected` only says a session was established — it stays True while
        the heartbeat keeps trying to rebuild a channel to a machine that has
        since been switched off. Callers deciding whether to *send* something
        (the folder watcher, the status badge) need this stricter answer.
        """
        return self.connected and self._writer is not None and self._reader is not None

    async def connect(self) -> MachineInfo:
        """Handshake, open the persistent data channel, start the heartbeat."""
        self.info.os_version = (await self._version()).strip()
        await self._identity()
        await self._open_data_channel()
        self.connected = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info("Connected to %s (%s)", self.info.name or self.ip, self.ip)
        return self.info

    async def disconnect(self) -> None:
        self.connected = False
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._data_lock:
            await self._close_data_channel()

    async def _version(self) -> str:
        reader, writer = await self._open(VERSION_PORT)
        try:
            writer.write(b"V\r\n")
            await writer.drain()
            return await self._read_line(reader)
        finally:
            await self._close(writer)

    async def _identity(self) -> None:
        reader, writer = await self._open(INFO_PORT)
        try:
            await self._ask(reader, writer, "Check Connectivity~2006")
            mi = await self._ask(reader, writer, "Machine Info")
            parts = mi.split("~")
            if len(parts) >= 3:
                self.info.name = parts[2].strip()
            self.info.machine_id = await self._ask(reader, writer, "Machine ID")
            self.info.serial = await self._ask(reader, writer, "Serial Number")
            units = await self._ask(reader, writer, "Machine Units~Read")
            if units:
                self.info.unit_convention = units.strip()
        finally:
            await self._close(writer)

    # ── data channel (persistent 2006 socket) ───────────────────────────────

    async def _open_data_channel(self) -> None:
        """Open the persistent 2006 socket, backing off through the ~7 s
        post-close cooldown, then send the one-time `app version` greeting."""
        deadline = asyncio.get_running_loop().time() + RECONNECT_DEADLINE
        delay = 0.25
        while True:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, DATA_PORT),
                    timeout=CONNECT_TIMEOUT,
                )
                self._send(self._writer, f"app version~{APP_VERSION}")
                await self._writer.drain()
                log.debug("2006 data channel open to %s", self.ip)
                return
            except (OSError, asyncio.TimeoutError) as e:
                self._reader = self._writer = None
                if asyncio.get_running_loop().time() >= deadline:
                    raise ConnectionError(
                        f"Cannot open data channel to {self.ip}:{DATA_PORT} — {e}"
                    ) from e
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 1.5)

    async def _close_data_channel(self) -> None:
        writer = self._writer
        self._reader = self._writer = None
        if writer is not None:
            await self._close(writer)

    async def _reconnect_locked(self) -> None:
        """Tear down and reopen the data channel. Caller must hold `_data_lock`."""
        await self._close_data_channel()
        try:
            await self._open_data_channel()
        except ConnectionError as e:
            log.warning("2006 reconnect failed: %s", e)

    async def _heartbeat_loop(self) -> None:
        """Keep the 2006 socket alive: `heartbeat` -> `handshake`, every few
        seconds, under the lock so it never collides with a data op.

        Bulletproof by design: any error (including a transport closed by a
        failed transfer) is swallowed and triggers a reconnect. The loop must
        never die while `connected`, or the channel would stay dead — so a bad
        download/upload can't brick subsequent listings.
        """
        while self.connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                async with self._data_lock:
                    if self._writer is None or self._reader is None:
                        await self._reconnect_locked()
                    reader, writer = self._reader, self._writer
                    if reader is None or writer is None:
                        continue
                    self._send(writer, HEARTBEAT_CMD)
                    await writer.drain()
                    await self._read_line(reader)  # expect "handshake"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — keepalive must survive anything
                log.warning("Heartbeat error (%s); reconnecting 2006 channel", e)
                try:
                    async with self._data_lock:
                        await self._reconnect_locked()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass

    async def _attempt(
        self, op: "Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[T]]"
    ) -> T:
        if self._writer is None or self._reader is None:
            await self._open_data_channel()
        reader, writer = self._reader, self._writer
        if reader is None or writer is None:
            raise ControllerError("Data channel unavailable.")
        return await op(reader, writer)

    async def _run_data_op(
        self, op: "Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[T]]"
    ) -> T:
        """Run a data op on the persistent socket under the lock. Logical
        rejections (`ControllerError`) propagate as-is; a transport failure
        triggers one transparent reconnect + retry, and on a second failure the
        channel is left closed so the heartbeat rebuilds it cleanly."""
        self._require_connected()
        async with self._data_lock:
            try:
                return await self._attempt(op)
            except ControllerError:
                raise
            except Exception as e:  # noqa: BLE001 — transport-level failure
                log.warning("Data op failed (%s); reconnecting 2006 channel", e)
                await self._reconnect_locked()
                try:
                    return await self._attempt(op)
                except ControllerError:
                    raise
                except Exception as e2:  # noqa: BLE001
                    await self._close_data_channel()
                    raise ControllerError(f"Data channel error: {e2}") from e2

    # ── data operations ────────────────────────────────────────────────────

    async def list_files(self, path: Optional[str] = None) -> list[RemoteFile]:
        """List the current (or given) directory on the controller."""
        if path:
            self.cwd = clamp_path_to_root(path)
        else:
            self.cwd = clamp_path_to_root(self.cwd)

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> str:
            self._send(writer, f"Job Filepath~{self.cwd}")
            await writer.drain()
            self._send(writer, "Get Files")
            await writer.drain()
            return await self._read_line(reader)

        reply = await self._run_data_op(op)
        return self._parse_file_list(reply)

    async def upload_file(self, filename: str, content: bytes) -> bool:
        """Write a .nc job file into the current directory. (Verified live.)"""
        self._validate_name(filename)
        path = self._join(filename)
        lines = content.decode("utf-8", errors="replace").splitlines()
        payload = "~".join(lines)

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> bool:
            ack = await self._ask(reader, writer, f"File Open~{path}")
            if not ack.startswith("OK"):
                raise ControllerError(f"File Open rejected: {ack!r}")
            self._send(writer, f"File Write~{payload}")
            self._send(writer, "Clear Status")
            await writer.drain()
            done = await self._read_line(reader)
            return done.startswith("OK")

        return await self._run_data_op(op)

    async def download_file(self, filename: str) -> bytes:
        """Read a file from the current directory back as bytes.

        Wire form: `File Open~<path>~Read` → `OK~<size>`, then repeated
        `File Read` streams the file as its lines joined by `~`, terminated by
        a trailing `~EOF\\r\\n`. The `<size>` is the *on-disk* size and is
        LARGER than the wire payload (newlines are 1-byte `~` on the wire, 2-byte
        `\\r\\n` on disk) — so we stop on the `EOF` sentinel, NOT the byte count.
        Looping on the byte count sends one `File Read` too many and the
        controller resets the socket. (`~` decoded back to `\\r\\n` newlines.)
        """
        path = self._join(filename)

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> bytes:
            ack = await self._ask(reader, writer, f"File Open~{path}~Read")
            if not ack.startswith("OK"):
                raise ControllerError(f"File Open rejected: {ack!r}")
            raw = bytearray()
            # Generous read cap so a malformed stream can't loop forever.
            for _ in range(100_000):
                self._send(writer, "File Read")
                await writer.drain()
                chunk = await asyncio.wait_for(
                    reader.read(65536), timeout=TRANSFER_TIMEOUT
                )
                if not chunk:
                    break
                raw.extend(chunk)
                if b"~EOF" in raw:
                    break
            # Finalize: `File Close` returns the controller to command mode.
            # Without it the controller stays stuck replying "Bad" to every
            # subsequent command. It then emits a short burst of trailing status
            # lines ("Bad"×N, "NA") — drain until the socket goes quiet so the
            # persistent channel is aligned for the next op.
            self._send(writer, "File Close")
            await writer.drain()
            await self._drain_quiet(reader)
            return self._decode_download(bytes(raw))

        return await self._run_data_op(op)

    async def select_file(self, filename: str) -> bool:
        """Set <filename> as the controller's current job — the right-click
        'Select' in the Windows app (`Select File~<path>` → `OK`). This only
        makes it the loaded job; the operator still presses Run at the machine.
        """
        path = self._join(filename)

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> bool:
            ack = await self._ask(reader, writer, f"Select File~{path}")
            return ack.startswith("OK")

        return await self._run_data_op(op)

    async def current_job(self) -> str:
        """Return the controller's currently-selected job path (`Current Path`)."""

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> str:
            return await self._ask(reader, writer, "Current Path")

        return await self._run_data_op(op)

    async def delete_item(self, name: str) -> bool:
        """Delete a file or folder in the current directory.

        Both go through the controller's single `Delete Item~<path>` command —
        the same one the original app's right-click 'Delete' used. File deletion
        is verified live. Folder deletion uses the identical command (the
        controller takes the path generically) but is NOT yet verified on a
        non-empty folder: if this unit only removes empty folders, the ack comes
        back non-OK and the caller surfaces a 'rejected' error — it never
        silently half-deletes. If live testing shows non-empty folders are
        refused, empty the folder first (recurse + delete children) before this.
        """
        path = self._join(name)

        async def op(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> bool:
            ack = await self._ask(reader, writer, f"Delete Item~{path}")
            return ack.startswith("OK")

        return await self._run_data_op(op)

    async def search_controllers(self) -> list[str]:
        """Scan the local /24 subnet for controllers answering on 1028."""
        prefix = ".".join(self.ip.split(".")[:3])
        candidates = [f"{prefix}.{i}" for i in range(1, 255)]
        results = await asyncio.gather(
            *(self._probe(ip) for ip in candidates), return_exceptions=True
        )
        return [ip for ip, ok in zip(candidates, results) if ok is True]

    # ── helpers ────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self.connected:
            raise ConnectionError("Not connected to controller.")

    @staticmethod
    def _validate_name(filename: str) -> None:
        if filename.startswith("_"):
            raise ControllerError("Invalid file name: cannot begin with '_'.")

    def _join(self, filename: str) -> str:
        return f"{self.cwd.rstrip(chr(92))}\\{filename}"

    async def _open(
        self, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self.ip, port), timeout=CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"Cannot reach {self.ip}:{port} — {e}") from e

    @staticmethod
    def _send(writer: asyncio.StreamWriter, cmd: str) -> None:
        writer.write(cmd.encode("utf-8") + b"\r\n")

    async def _ask(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cmd: str,
    ) -> str:
        self._send(writer, cmd)
        await writer.drain()
        return await self._read_line(reader)

    @staticmethod
    def _decode_download(raw: bytes) -> bytes:
        """Turn a raw `File Read` stream into file bytes.

        On the wire the file arrives as its lines joined by `~`, terminated by
        `~EOF` (with controller status noise after it). `~` decodes back to
        `\\r\\n` newlines.
        """
        i = raw.find(b"~EOF")
        payload = raw[:i] if i >= 0 else raw.rstrip(b"\r\n")
        lines = payload.decode("utf-8", errors="replace").split("~")
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")

    @staticmethod
    async def _drain_quiet(
        reader: asyncio.StreamReader, idle: float = 0.6, cap: int = 200
    ) -> None:
        """Consume buffered lines until the socket goes quiet (no new line within
        `idle` seconds). Used after a download's `File Close` to clear the
        controller's trailing status burst and realign the channel."""
        for _ in range(cap):
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=idle)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return

    @staticmethod
    async def _read_line(reader: asyncio.StreamReader) -> str:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n"), timeout=CMD_TIMEOUT
            )
        except asyncio.IncompleteReadError as e:
            raw = e.partial
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def _parse_file_list(self, reply: str) -> list[RemoteFile]:
        """Parse a `Get Files` reply.

        Format: `<dir>~<subdir [folder N files]>~...~<file>~<file>`.
        The first field echoes the listed directory. Subdirectory entries
        are full paths tagged ` [folder N files]`; everything else is a
        bare file name in the current directory.
        """
        parts = reply.split("~")
        out: list[RemoteFile] = []
        for entry in parts[1:]:
            entry = entry.strip()
            if not entry:
                continue
            if FOLDER_TAG in entry:
                full = entry[: entry.index(FOLDER_TAG)].rstrip()
                name = full.rstrip("\\").rsplit("\\", 1)[-1]
                out.append(RemoteFile(name=name, path=full, is_dir=True))
            else:
                out.append(
                    RemoteFile(name=entry, path=self._join(entry), is_dir=False)
                )
        return out

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    @classmethod
    async def _probe(cls, ip: str) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, VERSION_PORT), timeout=0.3
            )
            writer.close()
            return True
        except Exception:
            return False
