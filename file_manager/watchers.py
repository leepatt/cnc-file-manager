"""Folder watchers — drop a .nc in a watched folder, it lands on a machine.

Two machines, two transports, one framework:

  - **Multicam** (AXYZ A2MC controller) — files go over the controller's raw
    TCP protocol via `ControllerUploader` (see controller.py).
  - **Woodtron** — files go over anonymous FTP via `FtpUploader`. This replaces
    the standalone `WatchFolder WOODTRON.ps1` script, which only fired on
    filesystem `Created` events and so silently missed files that Google Drive
    materialised by other means.

Both watched folders live on Google Drive for Desktop (the `G:` drive), so the
team can drop a job from any computer anywhere and it reaches the machine in
the workshop. That shapes three design decisions:

  1. **Poll, don't subscribe.** Drive's virtual filesystem does not raise
     reliable `FileSystemWatcher` events — a file synced down from another
     computer often arrives with no `Created` event at all. We list the folder
     on a timer instead.
  2. **Wait for the file to settle.** A file is only sent once its size and
     mtime are unchanged across two consecutive polls, so a half-synced job is
     never pushed to a machine.
  3. **Tolerate the folder vanishing.** `G:` is not mounted until Google Drive
     starts, which can be a minute after login, and it can drop out later. A
     missing folder is a wait-and-retry condition, never a fatal error.

State (which files have been sent) is persisted per watcher so restarts don't
re-send. On the *first* ever run the folder's current contents are recorded as
already-sent ("seeding") rather than uploaded — otherwise standing up a new PC
would flood both machines with every job in the folder's history.
"""
from __future__ import annotations

import asyncio
import ftplib
import io
import json
import logging
import os
import re
import time
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol
from uuid import uuid4

log = logging.getLogger(__name__)

# Retry backoff for a file whose upload actually failed (as opposed to one
# waiting on a machine that is simply offline — that never counts as an
# attempt). Grows to a five-minute ceiling and stays there: we never give up
# permanently, because "the Woodtron was off all weekend" has to resolve itself
# on Monday without anyone touching the app.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 300.0
# Log noisily for the first few failures, then only every Nth, so a machine
# left off overnight doesn't write a gigabyte of identical warnings.
LOUD_ATTEMPTS = 3
QUIET_LOG_EVERY = 20
# Cap on remembered sent-files, newest kept. These folders are long-lived and
# never pruned by hand, so the state file needs a ceiling.
MAX_REMEMBERED = 5000
# Rolling per-watcher activity feed shown in the web UI.
MAX_EVENTS = 40
# Where a delivered file is moved to, inside the watched folder. A subfolder
# rather than a sibling so one Drive folder holds the whole story, and because
# the watcher's glob is non-recursive — files in here are never re-sent.
DEFAULT_ARCHIVE_DIR = "Sent"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# Windows spells its socket errors out at length — a failed connection arrives as
# "[WinError 10060] A connection attempt failed because the connected party did
# not properly respond after a period of time, or established connection failed
# because connected host has failed to respond". Accurate, and useless on a
# status card: it buries the one fact that matters under a paragraph. The full
# text still goes to the log.
_WINSOCK_BRIEF = {
    "10060": "no response",
    "10061": "connection refused",
    "10064": "host is down",
    "10065": "no route to host",
    "11001": "host not found",
}


def brief_error(e: BaseException) -> str:
    """Condense an exception to something that fits on one line of a card."""
    text = " ".join(str(e).split()) or e.__class__.__name__
    match = re.match(r"\[WinError (\d+)\]", text)
    if match:
        return _WINSOCK_BRIEF.get(match.group(1), f"WinError {match.group(1)}")
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return text if len(text) <= 90 else text[:87] + "..."


@dataclass
class WatchEvent:
    """One line in a watcher's activity feed."""

    time: str
    level: str  # "ok" | "error" | "info"
    message: str

    def as_dict(self) -> dict:
        return {"time": self.time, "level": self.level, "message": self.message}


class UploadRejected(Exception):
    """The destination refused this file and retrying won't help (bad name, etc.)."""


class Uploader(Protocol):
    """Where a watched file gets sent."""

    label: str
    target: str

    async def ready(self) -> tuple[bool, str]:
        """(can_send_now, human-readable reason when not)."""
        ...

    async def send(self, filename: str, content: bytes) -> None:
        """Deliver one file. Raise on failure, return None on success."""
        ...


# ── destinations ──────────────────────────────────────────────────────────────


class ControllerUploader:
    """Sends to the Multicam A2MC controller over its own TCP protocol.

    Delegates to the app's shared upload path rather than opening its own
    connection, because the controller allows exactly one data channel — the
    web UI, this watcher, and the keepalive heartbeat all share one socket.
    """

    def __init__(
        self,
        send_fn: "Callable[[str, bytes, str], Awaitable[bool]]",
        is_ready: "Callable[[], tuple[bool, str]]",
        subdir: str = "",
        label: str = "Multicam",
        target: str = "",
    ):
        self._send_fn = send_fn
        self._is_ready = is_ready
        self._subdir = subdir
        self.label = label
        self.target = target

    async def ready(self) -> tuple[bool, str]:
        return self._is_ready()

    async def send(self, filename: str, content: bytes) -> None:
        ok = await self._send_fn(filename, content, self._subdir)
        if not ok:
            raise RuntimeError("controller rejected the file")


class FtpUploader:
    """Sends to the Woodtron over plain FTP (anonymous, binary, passive).

    Mirrors what the old PowerShell script did with `WebClient.UploadFile`,
    which is what the Woodtron's FTP server expects. Every call opens and
    closes its own session, so the server is never left holding an idle
    connection and a session gone stale can't wedge the watcher.

    `ftplib` is blocking, so both `ready()` and `send()` run in a worker thread
    to keep the event loop free.
    """

    def __init__(
        self,
        host: str,
        remote_dir: str = "/",
        user: str = "anonymous",
        password: str = "anonymous@",
        port: int = 21,
        timeout: float = 60.0,
        probe_timeout: float = 5.0,
        label: str = "Woodtron",
    ):
        self.host = host
        self.port = port
        self.remote_dir = remote_dir or "/"
        self.user = user
        self.password = password
        # Transfers get a generous timeout — Woodtron job files run to tens of
        # megabytes. The "is it awake?" probe does not: an unreachable host makes
        # a TCP connect hang for ~21s on Windows before it gives up, and the
        # watcher's status would sit stale for that whole time on every poll.
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        self.label = label
        self.target = f"ftp://{host}{self.remote_dir}"

    def _connect(self, timeout: Optional[float] = None) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=timeout or self.timeout)
        ftp.login(self.user, self.password)
        ftp.set_pasv(True)
        if self.remote_dir not in ("", "/"):
            ftp.cwd(self.remote_dir)
        return ftp

    @staticmethod
    def _close(ftp: ftplib.FTP) -> None:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 — best-effort close, never mask the real error
            ftp.close()

    def _probe_sync(self) -> None:
        ftp = self._connect(timeout=self.probe_timeout)
        try:
            ftp.voidcmd("NOOP")
        finally:
            self._close(ftp)

    async def ready(self) -> tuple[bool, str]:
        try:
            await asyncio.to_thread(self._probe_sync)
        except Exception as e:  # noqa: BLE001 — an offline machine is normal
            log.debug("%s FTP probe to %s failed: %s", self.label, self.host, e)
            return False, f"{self.label} not reachable at {self.host} ({brief_error(e)})"
        return True, ""

    def _send_sync(self, filename: str, content: bytes) -> None:
        ftp = self._connect()
        try:
            ftp.storbinary(f"STOR {filename}", io.BytesIO(content))
        finally:
            self._close(ftp)

    async def send(self, filename: str, content: bytes) -> None:
        try:
            await asyncio.to_thread(self._send_sync, filename, content)
        except ftplib.error_perm as e:
            # A 5xx is a permanent refusal (bad name, no write permission);
            # resending the identical bytes will only fail the same way.
            raise UploadRejected(str(e)) from e


# ── the watcher ───────────────────────────────────────────────────────────────


def files_ready_to_upload(
    current: dict, last_seen: dict, sent: dict, deferred: Optional[dict] = None
) -> list[str]:
    """Pick which files to send this round. Pure — the unit-tested core.

    A file qualifies only when it is BOTH settled (its `[size, mtime]`
    signature matches the previous poll, so Drive has finished writing it) AND
    not already delivered at that exact signature. `deferred` maps a filename
    to `(retry_after_monotonic, signature)`, which is how a failed upload backs
    off without holding up every other file in the folder.
    """
    now = time.monotonic()
    ready: list[str] = []
    for name, sig in sorted(current.items()):
        if sent.get(name) == sig:
            continue  # already delivered, unchanged since
        if last_seen.get(name) != sig:
            continue  # brand new, or still being written — let it settle
        if deferred is not None:
            hold = deferred.get(name)
            # Only honour a backoff recorded against *this* version of the
            # file; an edited file earns an immediate fresh attempt.
            if hold is not None and hold[1] == sig and hold[0] > now:
                continue
        ready.append(name)
    return ready


def unique_target(folder: Path, name: str) -> Path:
    """A path in `folder` for `name` that doesn't clobber an existing file.

    The same job name comes back around — a recut, a revision, the same part
    next month — and the archive is the record of what actually went to the
    machine. Overwriting would quietly destroy that, so collisions get the
    familiar Windows-style " (2)" suffix instead.
    """
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = os.path.splitext(name)
    for n in range(2, 1000):
        candidate = folder / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    # A thousand copies of one name is absurd, but still not a reason to
    # overwrite: fall back to something guaranteed distinct.
    return folder / f"{stem} ({uuid4().hex[:8]}){suffix}"


@dataclass
class WatcherStats:
    scans: int = 0
    sent: int = 0
    archived: int = 0
    failed: int = 0
    last_sent_name: str = ""
    last_sent_at: str = ""
    last_error: str = ""
    last_error_at: str = ""
    waiting_reason: str = ""
    events: list[WatchEvent] = field(default_factory=list)


class FolderWatcher:
    """Polls one folder and ships new files to one destination.

    Never raises out of `run()` — a watcher that dies is a watcher nobody
    notices is dead, and this process is expected to stay up for months.
    """

    def __init__(
        self,
        key: str,
        directory: str,
        uploader: Uploader,
        state_path: Path,
        interval: float = 10.0,
        pattern: str = "*.nc",
        archive_dir: str = DEFAULT_ARCHIVE_DIR,
    ):
        self.key = key
        self.directory = Path(directory)
        self.uploader = uploader
        self.state_path = Path(state_path)
        self.interval = max(2.0, interval)
        self.pattern = pattern
        # Empty disables archiving: delivered files then stay put, as before.
        self.archive_dir = (archive_dir or "").strip()
        self.stats = WatcherStats()
        self._sent: dict = {}
        self._seeded = False
        self._last_seen: dict = {}
        self._deferred: dict = {}  # name -> (retry_after_monotonic, signature)
        self._attempts: dict = {}  # name -> consecutive failure count
        # Files this watcher genuinely delivered but has not yet moved into the
        # archive. Persisted, so a restart between the send and the move still
        # finishes the job — and so that files merely *seeded* on first run are
        # never moved, because we never actually sent those.
        self._pending_move: dict = {}
        self._load_state()

    @property
    def archive_path(self) -> Optional[Path]:
        return self.directory / self.archive_dir if self.archive_dir else None

    # ── state ────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        if "sent" in raw:
            sent = raw.get("sent") or {}
            self._sent = {k: list(v) for k, v in sent.items() if isinstance(v, list)}
            self._seeded = bool(raw.get("seeded"))
            pending = raw.get("pending_move") or {}
            self._pending_move = {
                k: list(v) for k, v in pending.items() if isinstance(v, list)
            }
        else:
            # Legacy watch_state.json: a bare {name: [size, mtime]} map written
            # by the pre-Woodtron single-watcher build. Adopt it as-is, and
            # treat it as seeded so an upgrade never re-sends the backlog.
            self._sent = {k: list(v) for k, v in raw.items() if isinstance(v, list)}
            self._seeded = True

    def _save_state(self) -> None:
        if len(self._sent) > MAX_REMEMBERED:
            # Keep the most recently modified entries; mtime is element 1.
            keep = sorted(self._sent.items(), key=lambda kv: kv[1][1], reverse=True)
            self._sent = dict(keep[:MAX_REMEMBERED])
        payload = {
            "version": 3,
            "seeded": self._seeded,
            "sent": self._sent,
            "pending_move": self._pending_move,
        }
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)  # atomic: never a half-written state
        except OSError as e:
            log.warning("%s: could not save watch state (%s)", self.key, e)

    # ── reporting ────────────────────────────────────────────────────────

    def _event(self, level: str, message: str) -> None:
        self.stats.events.insert(0, WatchEvent(_now_iso(), level, message))
        del self.stats.events[MAX_EVENTS:]

    def _folder_available(self) -> bool:
        try:
            return self.directory.is_dir()
        except OSError:
            return False

    def status(self) -> dict:
        # Name the waiting files, don't just count them. "3 waiting" prompts a
        # worried trip to the workshop; "3 waiting - Woodtron unreachable, and
        # here they are" is something an operator can act on.
        pending_names = sorted(
            name
            for name, sig in self._last_seen.items()
            if self._sent.get(name) != sig
        )
        return {
            "key": self.key,
            "label": self.uploader.label,
            "directory": str(self.directory),
            "target": self.uploader.target,
            "interval": self.interval,
            "folder_available": self._folder_available(),
            "pending": len(pending_names),
            "pending_names": pending_names[:20],
            "scans": self.stats.scans,
            "sent": self.stats.sent,
            "archived": self.stats.archived,
            "archive_dir": self.archive_dir,
            "awaiting_move": len(self._pending_move),
            "failed": self.stats.failed,
            "last_sent_name": self.stats.last_sent_name,
            "last_sent_at": self.stats.last_sent_at,
            "last_error": self.stats.last_error,
            "last_error_at": self.stats.last_error_at,
            "waiting_reason": self.stats.waiting_reason,
            "events": [e.as_dict() for e in self.stats.events],
        }

    # ── scanning ─────────────────────────────────────────────────────────

    def _scan(self) -> dict:
        """Signature every matching file in the folder. Runs off-thread."""
        current: dict = {}
        for entry in self.directory.glob(self.pattern):
            try:
                if not entry.is_file():
                    continue
                st = entry.stat()
            except OSError:
                continue  # vanished or still materialising — a later scan gets it
            current[entry.name] = [st.st_size, int(st.st_mtime)]
        return current

    def _seed(self, current: dict) -> None:
        self._sent = dict(current)
        self._seeded = True
        self._save_state()
        log.info(
            "%s: first run — %d existing file(s) in %s marked as already sent",
            self.key,
            len(current),
            self.directory,
        )
        self._event(
            "info",
            f"First run: {len(current)} existing file(s) ignored. "
            "Only new or changed files from here on.",
        )

    def _defer(self, name: str, sig: list) -> None:
        attempts = max(1, self._attempts.get(name, 1))
        delay = min(RETRY_BASE_SECONDS * (2 ** (attempts - 1)), RETRY_MAX_SECONDS)
        self._deferred[name] = (time.monotonic() + delay, sig)

    def _note_failure(self, name: str, sig: list, reason: str, permanent: bool) -> None:
        attempts = self._attempts.get(name, 0) + 1
        self._attempts[name] = attempts
        self.stats.failed += 1
        self.stats.last_error = f"{name}: {reason}"
        self.stats.last_error_at = _now_iso()
        if permanent:
            # Won't succeed on a retry — record it and stop asking. An edited
            # file gets a fresh signature and therefore a fresh chance.
            self._sent[name] = sig
            self._save_state()
            self._event("error", f"{name} rejected — {reason}. Not retrying.")
            log.warning("%s: %s rejected — %s (not retrying)", self.key, name, reason)
            return
        self._defer(name, sig)
        if attempts <= LOUD_ATTEMPTS or attempts % QUIET_LOG_EVERY == 0:
            log.warning(
                "%s: upload of %s failed (%s) — attempt %d, will retry",
                self.key,
                name,
                reason,
                attempts,
            )
            self._event("error", f"{name} failed ({reason}) — will retry")

    async def _send_one(self, name: str, sig: list) -> None:
        path = self.directory / name
        try:
            # Off-thread: an online-only Drive file hydrates (downloads) on
            # read, which blocks for as long as that download takes.
            content = await asyncio.to_thread(path.read_bytes)
        except OSError as e:
            self._note_failure(name, sig, f"cannot read file ({brief_error(e)})", permanent=False)
            return
        # Re-stat after reading: if Drive rewrote the file mid-read we hold a
        # torn copy, so drop it and let a later scan pick up the settled one.
        try:
            after = path.stat()
        except OSError as e:
            self._note_failure(name, sig, f"vanished while reading ({e})", permanent=False)
            return
        if [after.st_size, int(after.st_mtime)] != sig:
            log.info("%s: %s changed while reading — will resend once settled", self.key, name)
            return
        try:
            await self.uploader.send(name, content)
        except asyncio.CancelledError:
            raise
        except UploadRejected as e:
            self._note_failure(name, sig, brief_error(e), permanent=True)
            return
        except Exception as e:  # noqa: BLE001 — any transport failure is retryable
            log.debug("%s: upload of %s failed", self.key, name, exc_info=True)
            self._note_failure(name, sig, brief_error(e), permanent=False)
            return
        # Record the delivery BEFORE attempting the move. If the process dies
        # in between, the file is still marked sent (so it is never sent twice)
        # and still queued for archiving (so it doesn't linger forever).
        self._sent[name] = sig
        self._attempts.pop(name, None)
        self._deferred.pop(name, None)
        if self.archive_dir:
            self._pending_move[name] = sig
        self._save_state()
        self.stats.sent += 1
        self.stats.last_sent_name = name
        self.stats.last_sent_at = _now_iso()
        self._event("ok", f"Sent {name} ({len(content):,} bytes) to {self.uploader.label}")
        log.info("%s: sent %s (%d bytes)", self.key, name, len(content))

        if self.archive_dir:
            await self._archive_one(name)

    # ── archiving ────────────────────────────────────────────────────────

    def _archive_sync(self, name: str) -> str:
        """Move one delivered file into the archive folder. Runs off-thread.

        Order is deliberate and non-negotiable: the file must exist in the
        archive before it stops existing in the watched folder. `os.replace`
        via `shutil.move` is a rename — atomic within the same Drive folder, so
        there is no window where the file is in neither place. If Drive refuses
        the rename we fall back to copy-then-delete, and the delete only runs
        after the copy is verified. A job file is never at risk of vanishing.
        """
        archive = self.archive_path
        assert archive is not None  # guarded by the caller
        archive.mkdir(parents=True, exist_ok=True)
        source = self.directory / name
        target = unique_target(archive, name)
        size = source.stat().st_size
        try:
            shutil.move(str(source), str(target))
        except OSError:
            # Cross-device, or Drive declining the rename. Copy first, confirm
            # the bytes landed, and only then remove the original.
            shutil.copy2(str(source), str(target))
            if target.stat().st_size != size:
                target.unlink(missing_ok=True)
                raise OSError(
                    f"archive copy of {name} is the wrong size — original left in place"
                )
            source.unlink()
        return target.name

    async def _archive_one(self, name: str) -> bool:
        """Archive a delivered file. Returns True when it is no longer in the
        watched folder (moved, or already gone)."""
        if self.archive_path is None:
            self._pending_move.pop(name, None)
            return True
        if not (self.directory / name).exists():
            # Someone tidied it by hand, which is a perfectly good outcome.
            self._pending_move.pop(name, None)
            self._save_state()
            return True
        try:
            final = await asyncio.to_thread(self._archive_sync, name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a locked or syncing file retries
            log.warning("%s: could not move %s to %s (%s); will retry",
                        self.key, name, self.archive_dir, brief_error(e))
            self.stats.last_error = f"{name}: could not move to {self.archive_dir}"
            self.stats.last_error_at = _now_iso()
            return False
        self._pending_move.pop(name, None)
        self._save_state()
        self.stats.archived += 1
        moved_note = "" if final == name else f" as {final}"
        self._event("ok", f"Moved {name} to {self.archive_dir}{moved_note}")
        log.info("%s: moved %s to %s%s", self.key, name, self.archive_dir, moved_note)
        return True

    async def _drain_pending_moves(self, current: dict) -> None:
        """Retry archiving anything delivered earlier but still sitting here.

        Covers a restart between send and move, a file briefly locked by Drive,
        and any other reason the move didn't happen first time. Self-healing:
        the folder converges on holding only un-sent work.
        """
        for name in list(self._pending_move):
            if name in current or (self.directory / name).exists():
                await self._archive_one(name)
            else:
                self._pending_move.pop(name, None)
                self._save_state()

    async def _tick(self) -> None:
        if not self._folder_available():
            self.stats.waiting_reason = (
                f"Folder not available yet: {self.directory} "
                "(Google Drive may still be starting)"
            )
            return
        current = await asyncio.to_thread(self._scan)
        self.stats.scans += 1
        if not self._seeded:
            self._seed(current)
            self._last_seen = current
            self.stats.waiting_reason = ""
            return
        # Finish any archiving left over from a previous round before deciding
        # what to send, so a file that was delivered but not yet moved doesn't
        # look like outstanding work.
        if self._pending_move:
            await self._drain_pending_moves(current)

        ready = files_ready_to_upload(current, self._last_seen, self._sent, self._deferred)
        self._last_seen = current
        if not ready:
            self.stats.waiting_reason = ""
            return
        can_send, reason = await self.uploader.ready()
        if not can_send:
            # The destination is down. That's not a failure of these files —
            # don't burn a retry attempt, just say so and try again next tick.
            #
            # The reason carries no count: `pending` already reports that, and
            # every display of this showed the number twice.
            self.stats.waiting_reason = reason
            return
        self.stats.waiting_reason = ""
        for name in ready:
            sig = current.get(name)
            if sig is None:
                continue
            await self._send_one(name, sig)

    async def run(self) -> None:
        log.info(
            "%s: watching %s every %.0fs -> %s",
            self.key,
            self.directory,
            self.interval,
            self.uploader.target or self.uploader.label,
        )
        # Short on purpose: the card already shows the folder, and repeating a
        # 90-character Drive path in the activity feed pushed everything else
        # out of view.
        self._event("info", f"Watching for new .nc files -> {self.uploader.label}")
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the loop must outlive anything
                log.warning("%s: watch loop error (%s)", self.key, e)
                self.stats.last_error = str(e)
                self.stats.last_error_at = _now_iso()
