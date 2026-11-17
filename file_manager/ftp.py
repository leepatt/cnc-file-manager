"""FTP plumbing for the Woodtron.

The Woodtron runs a **Windows CE 7.0** FTP server, which is about as minimal as
FTP gets. Probed live against the shop's machine (2026-08-26):

    SYST  -> "215 Windows_CE version 7.0."
    FEAT  -> 500 Syntax error, command unrecognized.
    MLSD  -> 500 Syntax error, command unrecognized.
    SIZE  -> 500 Syntax error, command unrecognized.
    MDTM  -> 500 Syntax error, command unrecognized.
    LIST  -> works, MS-DOS format
    NLST / CWD / RETR / STOR / DELE -> all work

So: no machine-readable listing (`MLSD`) and no per-file metadata commands.
Everything about a file — size, timestamp, whether it's a folder — has to come
from parsing the human-readable `LIST` output. That's the fragile part of FTP,
which is why `parse_list_line` is pure and directly unit-tested against real
captured output rather than being exercised only through a live machine.

`ftplib` is blocking, so callers run these off the event loop with
`asyncio.to_thread`.
"""
from __future__ import annotations

import ftplib
import io
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# MS-DOS style, which is what Windows CE emits:
#   08-19-26  14:07                26371 959 NEST (2 on 1)_S1 Q2.nc
#   05-01-24  08:18       <DIR>          AutoLabel
# Note the name runs to end-of-line and routinely contains spaces, brackets and
# parentheses, so it can only be captured last and greedily.
_DOS_LINE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})\s+"
    r"(?P<size><DIR>|\d+)\s+(?P<name>.+)$"
)

# Unix style, in case this machine is ever replaced by something more usual:
#   -rw-r--r--   1 owner group      26371 Aug 19 14:07 file.nc
_UNIX_LINE = re.compile(
    r"^(?P<perm>[-dl])[rwxstST-]{9}\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+(?P<mon>\w{3})\s+(?P<day>\d{1,2})\s+(?P<when>[\d:]{4,5})\s+(?P<name>.+)$"
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1
    )
}


@dataclass
class FtpEntry:
    name: str
    size: int = 0
    modified: str = ""
    is_dir: bool = False


def parse_list_line(line: str) -> Optional[FtpEntry]:
    """Turn one line of `LIST` output into an entry, or None if unrecognised.

    Handles the MS-DOS format this machine actually speaks, and the Unix format
    as a fallback. Returns None rather than raising: one odd line (a server
    banner, a locale quirk) must not blank out the whole listing.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

    m = _DOS_LINE.match(line)
    if m:
        name = m.group("name").strip()
        if name in (".", ".."):
            return None
        raw_size = m.group("size")
        is_dir = raw_size == "<DIR>"
        # MM-DD-YY. A two-digit year below 70 is this century — these machines
        # were not filing jobs in 1970.
        mm, dd, yy = m.group("date").split("-")
        year = int(yy) + (2000 if int(yy) < 70 else 1900)
        return FtpEntry(
            name=name,
            size=0 if is_dir else int(raw_size),
            modified=f"{year:04d}-{int(mm):02d}-{int(dd):02d} {m.group('time')}",
            is_dir=is_dir,
        )

    m = _UNIX_LINE.match(line)
    if m:
        name = m.group("name").strip()
        if name in (".", ".."):
            return None
        is_dir = m.group("perm") == "d"
        when = m.group("when")
        month = _MONTHS.get(m.group("mon"), 0)
        # Unix listings give a time for recent files and a year for older ones.
        if ":" in when:
            stamp = f"{month:02d}-{int(m.group('day')):02d} {when}"
        else:
            stamp = f"{when}-{month:02d}-{int(m.group('day')):02d}"
        return FtpEntry(
            name=name,
            size=0 if is_dir else int(m.group("size")),
            modified=stamp,
            is_dir=is_dir,
        )

    log.debug("Unrecognised FTP LIST line: %r", line)
    return None


def normalize_ftp_path(path: str, root: str = "/") -> str:
    """Resolve a browse path, refusing to escape the root.

    `..` is resolved rather than rejected outright, so a stray segment in a
    breadcrumb behaves sanely, but the result can never climb above `root`.
    """
    root = "/" + root.strip("/") if root.strip("/") else "/"
    raw = (path or root).replace("\\", "/")
    if not raw.startswith("/"):
        raw = root.rstrip("/") + "/" + raw

    parts: list[str] = []
    for segment in raw.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)

    resolved = "/" + "/".join(parts)
    root_parts = [p for p in root.split("/") if p]
    if resolved.strip("/").split("/")[: len(root_parts)] != root_parts:
        return root
    return resolved if resolved != "//" else "/"


class FtpClient:
    """A short-lived FTP session. Blocking — call from a worker thread.

    One session per operation, deliberately. This server drops idle
    connections without warning, and a stale handle that fails mid-listing is
    far more confusing than simply reconnecting each time. Operations here take
    well under a second on the shop LAN.
    """

    def __init__(
        self,
        host: str,
        port: int = 21,
        user: str = "anonymous",
        password: str = "anonymous@",
        timeout: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout

    def _open(self, timeout: Optional[float] = None) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=timeout or self.timeout)
        ftp.login(self.user, self.password)
        ftp.set_pasv(True)
        return ftp

    @staticmethod
    def _close(ftp: ftplib.FTP) -> None:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 — best-effort; never mask the real error
            ftp.close()

    def _session(self, fn: "Callable[[ftplib.FTP], object]", timeout: Optional[float] = None):
        ftp = self._open(timeout)
        try:
            return fn(ftp)
        finally:
            self._close(ftp)

    # ── operations ───────────────────────────────────────────────────────

    def noop(self, timeout: Optional[float] = None) -> None:
        """Cheap 'are you awake?' check."""
        self._session(lambda ftp: ftp.voidcmd("NOOP"), timeout=timeout)

    def list_dir(self, path: str = "/") -> list[FtpEntry]:
        def op(ftp: ftplib.FTP) -> list[FtpEntry]:
            # CWD then a bare LIST, rather than `LIST <path>`: both work on this
            # server, but a path containing spaces is ambiguous as a LIST
            # argument and job names here are full of them.
            if path and path != "/":
                ftp.cwd(path)
            lines: list[str] = []
            ftp.retrlines("LIST", lines.append)
            entries = [parse_list_line(line) for line in lines]
            return [e for e in entries if e is not None]

        return self._session(op)  # type: ignore[return-value]

    def retrieve(self, path: str, filename: str) -> bytes:
        def op(ftp: ftplib.FTP) -> bytes:
            if path and path != "/":
                ftp.cwd(path)
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {filename}", buf.write)
            return buf.getvalue()

        return self._session(op)  # type: ignore[return-value]

    def store(self, path: str, filename: str, content: bytes) -> None:
        def op(ftp: ftplib.FTP) -> None:
            if path and path != "/":
                ftp.cwd(path)
            ftp.storbinary(f"STOR {filename}", io.BytesIO(content))

        self._session(op)

    def delete(self, path: str, name: str, is_dir: bool = False) -> None:
        def op(ftp: ftplib.FTP) -> None:
            if path and path != "/":
                ftp.cwd(path)
            if is_dir:
                ftp.rmd(name)
            else:
                ftp.delete(name)

        self._session(op)
