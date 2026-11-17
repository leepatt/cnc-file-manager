"""Folder-watcher tests — the auto-send path for both machines.

No machine and no Google Drive needed: the folder is a tmp_path and the
destination is a fake uploader that records what it was handed. What matters
here is the decision logic, because getting it wrong means either a job that
never arrives or a half-synced file cut on a real machine.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from file_manager.watchers import (
    FolderWatcher,
    UploadRejected,
    brief_error,
    files_ready_to_upload,
)


class FakeUploader:
    """Records every delivery; can be made unavailable or made to fail."""

    label = "Fake"
    target = "fake://test"

    def __init__(self, ready: bool = True, fail: Exception | None = None):
        self.sent: list[tuple[str, bytes]] = []
        self._ready = ready
        self._fail = fail
        self.ready_calls = 0

    async def ready(self) -> tuple[bool, str]:
        self.ready_calls += 1
        return (True, "") if self._ready else (False, "machine is off")

    async def send(self, filename: str, content: bytes) -> None:
        if self._fail is not None:
            raise self._fail
        self.sent.append((filename, content))


def make_watcher(tmp_path: Path, uploader: FakeUploader, **kw) -> FolderWatcher:
    folder = tmp_path / "drop"
    folder.mkdir(exist_ok=True)
    return FolderWatcher(
        key="test",
        directory=str(folder),
        uploader=uploader,  # type: ignore[arg-type]
        state_path=tmp_path / "state.json",
        interval=2.0,
        **kw,
    )


def drop(watcher: FolderWatcher, name: str, body: bytes = b"G0 X0 Y0") -> Path:
    path = watcher.directory / name
    path.write_bytes(body)
    return path


# ── the pure selection rule ─────────────────────────────────────────────────


def test_waits_for_a_file_to_settle_then_sends_once() -> None:
    sent: dict = {}
    current = {"a.nc": [100, 5]}
    # First sighting — not in last_seen yet, so not settled: not ready.
    assert files_ready_to_upload(current, {}, sent) == []
    # Seen again unchanged — settled and undelivered: ready.
    assert files_ready_to_upload(current, current, sent) == ["a.nc"]
    # Once recorded as delivered at this size+mtime: never re-sent.
    sent["a.nc"] = [100, 5]
    assert files_ready_to_upload(current, current, sent) == []
    # Edited file (new size/mtime), now settled: ready again.
    changed = {"a.nc": [120, 9]}
    assert files_ready_to_upload(changed, changed, sent) == ["a.nc"]


def test_ignores_a_file_that_is_still_growing() -> None:
    # Size grew between polls (Drive still syncing it down): not ready.
    assert files_ready_to_upload({"a.nc": [200, 5]}, {"a.nc": [100, 5]}, {}) == []


def test_backoff_holds_a_failed_file_but_not_its_neighbours() -> None:
    current = {"a.nc": [1, 1], "b.nc": [2, 2]}
    deferred = {"a.nc": (time.monotonic() + 60, [1, 1])}
    assert files_ready_to_upload(current, current, {}, deferred) == ["b.nc"]


def test_backoff_is_dropped_when_the_file_is_edited() -> None:
    """A backoff is recorded against one version of a file. Save a fix and it
    must go straight away — nobody should wait out a five-minute timer they
    can't see because they corrected the problem."""
    deferred = {"a.nc": (time.monotonic() + 300, [1, 1])}
    edited = {"a.nc": [9, 9]}
    assert files_ready_to_upload(edited, edited, {}, deferred) == ["a.nc"]


def test_expired_backoff_lets_the_file_through() -> None:
    deferred = {"a.nc": (time.monotonic() - 1, [1, 1])}
    current = {"a.nc": [1, 1]}
    assert files_ready_to_upload(current, current, {}, deferred) == ["a.nc"]


# ── first run seeding ───────────────────────────────────────────────────────


def test_first_run_seeds_existing_files_instead_of_sending_them(tmp_path: Path) -> None:
    """Standing up a new PC must not push the folder's whole history at a machine."""
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    drop(w, "old1.nc")
    drop(w, "old2.nc")

    asyncio.run(w._tick())
    assert up.sent == []
    assert json.loads((tmp_path / "state.json").read_text())["seeded"] is True

    # A genuinely new file still goes, once it has settled.
    drop(w, "new.nc")
    asyncio.run(w._tick())   # sighted, not yet settled
    assert up.sent == []
    asyncio.run(w._tick())   # settled -> sent
    assert [n for n, _ in up.sent] == ["new.nc"]


def test_state_survives_a_restart(tmp_path: Path) -> None:
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    drop(w, "job.nc")
    asyncio.run(w._tick())          # seed
    drop(w, "later.nc")
    asyncio.run(w._tick())
    asyncio.run(w._tick())
    assert [n for n, _ in up.sent] == ["later.nc"]

    # A fresh watcher over the same state file must not re-send anything.
    up2 = FakeUploader()
    w2 = make_watcher(tmp_path, up2)
    asyncio.run(w2._tick())
    asyncio.run(w2._tick())
    assert up2.sent == []


def test_legacy_state_file_is_adopted_without_resending(tmp_path: Path) -> None:
    """The pre-Woodtron build wrote a bare {name: [size, mtime]} map."""
    folder = tmp_path / "drop"
    folder.mkdir()
    body = b"G0 X0 Y0"
    (folder / "already.nc").write_bytes(body)
    sig = (folder / "already.nc").stat()
    (tmp_path / "state.json").write_text(
        json.dumps({"already.nc": [sig.st_size, int(sig.st_mtime)]})
    )

    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    asyncio.run(w._tick())
    assert up.sent == []


# ── destination availability ────────────────────────────────────────────────


def test_offline_machine_holds_files_without_counting_a_failure(tmp_path: Path) -> None:
    up = FakeUploader(ready=False)
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())          # seed empty folder
    drop(w, "job.nc")
    asyncio.run(w._tick())
    asyncio.run(w._tick())

    assert up.sent == []
    assert w.stats.failed == 0, "a powered-down machine is not a failed upload"
    assert "machine is off" in w.stats.waiting_reason

    # Machine comes back — the queued file goes, no intervention needed.
    up._ready = True
    asyncio.run(w._tick())
    assert [n for n, _ in up.sent] == ["job.nc"]


def test_missing_folder_is_reported_not_fatal(tmp_path: Path) -> None:
    """G: isn't mounted until Google Drive starts, which can be after logon."""
    up = FakeUploader()
    w = FolderWatcher(
        key="test",
        directory=str(tmp_path / "not-there-yet"),
        uploader=up,  # type: ignore[arg-type]
        state_path=tmp_path / "state.json",
        interval=2.0,
    )
    asyncio.run(w._tick())
    status = w.status()
    assert status["folder_available"] is False
    assert "not available" in status["waiting_reason"]
    assert w.stats.failed == 0


# ── failure handling ────────────────────────────────────────────────────────


def test_transient_failure_is_retried_after_a_backoff(tmp_path: Path) -> None:
    up = FakeUploader(fail=RuntimeError("connection reset"))
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    drop(w, "job.nc")
    asyncio.run(w._tick())
    asyncio.run(w._tick())

    assert w.stats.failed == 1
    assert "job.nc" in w.stats.last_error
    # Held back rather than hammered on the very next poll.
    asyncio.run(w._tick())
    assert w.stats.failed == 1

    # Once the backoff lapses and the fault clears, it sends.
    w._deferred.clear()
    up._fail = None
    asyncio.run(w._tick())
    assert [n for n, _ in up.sent] == ["job.nc"]


def test_permanent_rejection_is_not_retried(tmp_path: Path) -> None:
    up = FakeUploader(fail=UploadRejected("553 file name not allowed"))
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    drop(w, "_bad.nc")
    asyncio.run(w._tick())
    asyncio.run(w._tick())

    assert w.stats.failed == 1
    before = w.stats.failed
    for _ in range(3):
        asyncio.run(w._tick())
    assert w.stats.failed == before, "a permanent rejection must stop asking"


def test_a_watcher_tick_never_raises(tmp_path: Path) -> None:
    """`run()` must outlive anything — a dead watcher is one nobody notices."""

    class Exploding(FakeUploader):
        async def ready(self):
            raise RuntimeError("boom")

    up = Exploding()
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    drop(w, "job.nc")
    asyncio.run(w._tick())

    async def one_pass() -> None:
        task = asyncio.create_task(w.run())
        await asyncio.sleep(2.3)   # long enough for a single interval tick
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(one_pass())
    assert w.stats.last_error  # recorded, not raised


# ── archiving delivered files ───────────────────────────────────────────────


def send_one(w: FolderWatcher, name: str, body: bytes = b"G0 X0 Y0") -> None:
    """Seed (if needed), drop a file, and run enough ticks to deliver it."""
    if not w._seeded:
        asyncio.run(w._tick())
    drop(w, name, body)
    asyncio.run(w._tick())   # sighted
    asyncio.run(w._tick())   # settled -> sent


def test_delivered_file_moves_to_sent_and_leaves_the_main_folder(tmp_path: Path) -> None:
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    send_one(w, "job.nc")

    assert [n for n, _ in up.sent] == ["job.nc"]
    assert (w.directory / "Sent" / "job.nc").exists(), "must land in Sent"
    assert not (w.directory / "job.nc").exists(), "must be gone from the main folder"
    assert w.stats.archived == 1


def test_archived_file_is_not_sent_again(tmp_path: Path) -> None:
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    send_one(w, "job.nc")
    for _ in range(3):
        asyncio.run(w._tick())
    assert len(up.sent) == 1, "the Sent subfolder must not be re-scanned"


def test_a_repeat_of_the_same_name_does_not_overwrite_the_record(tmp_path: Path) -> None:
    """Recuts and revisions reuse job names; the archive is the record of what
    actually went to the machine, so nothing in it may be silently destroyed."""
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    send_one(w, "job.nc", b"FIRST")
    send_one(w, "job.nc", b"SECOND VERSION")

    archived = sorted(p.name for p in (w.directory / "Sent").iterdir())
    assert archived == ["job (2).nc", "job.nc"]
    bodies = {p.read_bytes() for p in (w.directory / "Sent").iterdir()}
    assert bodies == {b"FIRST", b"SECOND VERSION"}


def test_a_file_that_failed_to_send_is_not_moved(tmp_path: Path) -> None:
    """Only delivery earns the move — otherwise a failure would look like a success."""
    up = FakeUploader(fail=RuntimeError("connection reset"))
    w = make_watcher(tmp_path, up)
    send_one(w, "job.nc")

    assert (w.directory / "job.nc").exists(), "must stay put for the retry"
    assert not (w.directory / "Sent").exists() or not list((w.directory / "Sent").iterdir())
    assert w.stats.archived == 0


def test_seeded_files_are_never_moved(tmp_path: Path) -> None:
    """First-run seeding marks existing files as sent, but we never actually
    sent them — moving them would misrepresent what reached the machine."""
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    drop(w, "old1.nc")
    drop(w, "old2.nc")
    asyncio.run(w._tick())   # seed
    asyncio.run(w._tick())
    asyncio.run(w._tick())

    assert (w.directory / "old1.nc").exists()
    assert (w.directory / "old2.nc").exists()
    assert up.sent == []
    assert w.stats.archived == 0


def test_a_failed_move_is_retried_and_never_loses_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the move fails, the file must remain in the main folder — present and
    intact — and be moved on a later pass."""
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())

    boom = {"fail": True}
    real = w._archive_sync

    def flaky(name: str) -> str:
        if boom["fail"]:
            raise OSError("file is locked by Google Drive")
        return real(name)

    monkeypatch.setattr(w, "_archive_sync", flaky)

    drop(w, "job.nc", b"PRECIOUS")
    asyncio.run(w._tick())
    asyncio.run(w._tick())

    assert [n for n, _ in up.sent] == ["job.nc"], "it did reach the machine"
    assert (w.directory / "job.nc").read_bytes() == b"PRECIOUS", "still here, intact"
    assert w._pending_move.get("job.nc"), "queued for another attempt"

    # Drive lets go; the next scan finishes the job without re-sending.
    boom["fail"] = False
    asyncio.run(w._tick())
    assert not (w.directory / "job.nc").exists()
    assert (w.directory / "Sent" / "job.nc").read_bytes() == b"PRECIOUS"
    assert len(up.sent) == 1, "must not be sent a second time"


def test_pending_move_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash between the send and the move must not strand the file."""
    up = FakeUploader()
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    monkeypatch.setattr(w, "_archive_sync", lambda name: (_ for _ in ()).throw(OSError("locked")))
    drop(w, "job.nc")
    asyncio.run(w._tick())
    asyncio.run(w._tick())
    assert (w.directory / "job.nc").exists()

    # Fresh watcher over the same state file, as after a restart.
    up2 = FakeUploader()
    w2 = make_watcher(tmp_path, up2)
    assert "job.nc" in w2._pending_move
    asyncio.run(w2._tick())
    assert (w2.directory / "Sent" / "job.nc").exists()
    assert up2.sent == [], "finishing the move must not re-send it"


def test_archiving_can_be_turned_off(tmp_path: Path) -> None:
    up = FakeUploader()
    w = make_watcher(tmp_path, up, archive_dir="")
    send_one(w, "job.nc")
    assert (w.directory / "job.nc").exists(), "left in place when archiving is off"
    assert not (w.directory / "Sent").exists()


def test_status_names_the_waiting_files(tmp_path: Path) -> None:
    """A count alone ("3 waiting") isn't actionable — the names are."""
    up = FakeUploader(ready=False)
    w = make_watcher(tmp_path, up)
    asyncio.run(w._tick())
    drop(w, "b.nc")
    drop(w, "a.nc")
    asyncio.run(w._tick())
    status = w.status()
    assert status["pending"] == 2
    assert status["pending_names"] == ["a.nc", "b.nc"]


# ── error messages fit on a card ────────────────────────────────────────────


def test_winsock_errors_are_condensed() -> None:
    """Windows spells a failed connect out over two lines; the card has one."""
    verbose = OSError(
        "[WinError 10060] A connection attempt failed because the connected party "
        "did not properly respond after a period of time, or established connection "
        "failed because connected host has failed to respond"
    )
    assert brief_error(verbose) == "no response"
    assert brief_error(OSError("[WinError 10061] No connection could be made")) == (
        "connection refused"
    )
    # An unmapped code still collapses to something short rather than a paragraph.
    assert brief_error(OSError("[WinError 12345] " + "x" * 300)) == "WinError 12345"


def test_ordinary_errors_pass_through_but_are_capped() -> None:
    assert brief_error(ValueError("bad name")) == "bad name"
    assert brief_error(asyncio.TimeoutError()) == "timed out"
    long = brief_error(RuntimeError("y" * 300))
    assert len(long) == 90 and long.endswith("...")
    # Newlines would break the single-line layout.
    assert brief_error(RuntimeError("two\nlines")) == "two lines"
