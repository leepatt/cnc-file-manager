"""Route-level tests against the FastAPI app with no controller connected.

The network-facing controller ops aren't exercised here (no machine on the LAN
in CI); these cover the connection-state gating, health contract, and the
machines save/load round-trip.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from file_manager import app as app_module
from file_manager.app import app
from file_manager.controller import DEFAULT_DIR


@pytest.fixture(autouse=True)
def reset_client() -> Iterator[None]:
    """Ensure each test starts with no active controller connection."""
    app_module._client = None
    yield
    app_module._client = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ── health + status ─────────────────────────────────────────────────────────


def test_health_ok_when_disconnected(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "file-manager"
    assert body["controller"]["connected"] is False


def test_status_reports_disconnected(client: TestClient) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {"connected": False, "busy": False, "busy_detail": ""}


# ── connection gating (409 when no controller) ──────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/files"),
        ("get", "/api/download/part1.nc"),
        ("post", "/api/load/part1.nc"),
        ("delete", "/api/files/part1.nc"),
    ],
)
def test_ops_require_connection(client: TestClient, method: str, path: str) -> None:
    r = getattr(client, method)(path)
    assert r.status_code == 409


def test_upload_requires_connection(client: TestClient) -> None:
    r = client.post(
        "/api/upload",
        files={"file": ("part1.nc", b"G0 X0 Y0", "application/octet-stream")},
    )
    assert r.status_code == 409


def test_upload_targets_jobs_root_and_restores_browse_cwd() -> None:
    class FakeClient:
        connected = True
        cwd = r"\hard disk2\release\jobs\PROD"

        async def upload_file(self, filename: str, content: bytes) -> bool:
            assert self.cwd == DEFAULT_DIR
            assert filename == "part1.nc"
            return True

        async def disconnect(self) -> None:
            self.connected = False

    fake = FakeClient()
    app_module._client = fake
    with TestClient(app) as client:
        r = client.post(
            "/api/upload",
            files={"file": ("part1.nc", b"G0 X0 Y0", "application/octet-stream")},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["directory"] == DEFAULT_DIR
    assert fake.cwd == r"\hard disk2\release\jobs\PROD"


def test_upload_with_dir_targets_subfolder_and_restores_browse_cwd() -> None:
    class FakeClient:
        connected = True
        cwd = r"\hard disk2\release\jobs\PROD"

        async def upload_file(self, filename: str, content: bytes) -> bool:
            assert self.cwd == DEFAULT_DIR + r"\WEB-GENERATED\boxmaker"
            assert filename == "part1.nc"
            return True

        async def disconnect(self) -> None:
            self.connected = False

    fake = FakeClient()
    app_module._client = fake
    with TestClient(app) as client:
        r = client.post(
            "/api/upload",
            files={"file": ("part1.nc", b"G0 X0 Y0", "application/octet-stream")},
            data={"dir": "WEB-GENERATED/boxmaker"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["directory"] == DEFAULT_DIR + r"\WEB-GENERATED\boxmaker"
    assert fake.cwd == r"\hard disk2\release\jobs\PROD"


def test_upload_rejects_traversal_in_dir() -> None:
    class FakeClient:
        connected = True
        cwd = DEFAULT_DIR

        async def upload_file(self, filename: str, content: bytes) -> bool:
            raise AssertionError("upload_file must not run for a rejected dir")

        async def disconnect(self) -> None:
            self.connected = False

    fake = FakeClient()
    app_module._client = fake
    with TestClient(app) as client:
        r = client.post(
            "/api/upload",
            files={"file": ("part1.nc", b"G0 X0 Y0", "application/octet-stream")},
            data={"dir": "../escape"},
        )
    assert r.status_code == 422
    assert fake.cwd == DEFAULT_DIR


def test_files_returns_stale_cache_when_controller_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_files_cache",
        {
            "cwd": DEFAULT_DIR,
            "root": DEFAULT_DIR,
            "files": [{"name": "part1.nc", "size": 1, "modified": "", "path": "", "is_dir": False}],
            "busy": False,
            "stale": False,
            "busy_detail": "",
        },
    )
    class FakeClient:
        connected = True
        busy_detail = "Uploading part2.nc"  # an upload is in flight

        async def disconnect(self) -> None:
            self.connected = False

    app_module._client = FakeClient()
    with TestClient(app) as client:
        r = client.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["busy"] is True
    assert body["files"][0]["name"] == "part1.nc"
    assert body["busy_detail"] == "Uploading part2.nc"


def test_files_does_live_read_when_not_uploading() -> None:
    # A held heartbeat (no busy_detail) must NOT be treated as busy — the
    # request should do a real listing rather than serve a stale/empty cache.
    class FakeClient:
        connected = True
        busy_detail = ""
        cwd = DEFAULT_DIR

        async def list_files(self, path):
            from file_manager.controller import RemoteFile
            return [RemoteFile(name="live.nc", size=2, path="", is_dir=False)]

        async def disconnect(self) -> None:
            self.connected = False

    app_module._client = FakeClient()
    app_module._files_cache = None
    with TestClient(app) as client:
        r = client.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is False
    assert body["files"][0]["name"] == "live.nc"


def test_controller_ready_reports_why_it_cannot_send() -> None:
    """The watcher's gate: each 'no' has to name a cause the operator can act on."""
    app_module._client = None
    ok, reason = app_module._controller_ready()
    assert ok is False and "not connected" in reason

    class FakeClient:
        connected = True
        ip = "192.168.0.100"
        busy_detail = ""
        channel_open = False

    app_module._client = FakeClient()
    ok, reason = app_module._controller_ready()
    assert ok is False and "data channel" in reason

    FakeClient.channel_open = True
    FakeClient.busy_detail = "Uploading big.nc"
    ok, reason = app_module._controller_ready()
    assert ok is False and "busy" in reason

    FakeClient.busy_detail = ""
    assert app_module._controller_ready() == (True, "")


def test_watchers_endpoint_lists_configured_watchers(client: TestClient) -> None:
    r = client.get("/api/watchers")
    assert r.status_code == 200
    assert isinstance(r.json()["watchers"], list)


def test_health_reports_watchers(client: TestClient) -> None:
    body = client.get("/health").json()
    assert "watchers" in body
    assert body["controller"]["link_ok"] is False


# ── multi-machine file browser ──────────────────────────────────────────────
# Fake backends throughout: these cover the routing and capability rules, not
# the machines. Nothing here touches the network.


class FakeBackend:
    path_sep = "/"

    def __init__(self, key: str, label: str, *, can_load: bool = False, online: bool = True):
        self.key = key
        self.label = label
        self.root = "/"
        self._can_load = can_load
        self._online = online
        self.deleted: list[tuple] = []

    def capabilities(self) -> dict:
        return {"folders": True, "upload": True, "download": True,
                "delete": True, "load": self._can_load, "sizes": True,
                "timestamps": True}

    async def status(self) -> dict:
        return {"online": self._online, "detail": "fake", "busy": False}

    async def list_files(self, path):
        from file_manager.controller import RemoteFile
        return [RemoteFile(name="a.nc", size=5, modified="2026-01-01 00:00",
                           path="/a.nc", is_dir=False)], (path or "/")

    async def upload(self, filename, content, path=""):
        return True

    async def download(self, filename, path=""):
        return b"G0"

    async def delete(self, name, path="", is_dir=False):
        self.deleted.append((name, path, is_dir))
        return True

    async def load(self, filename, path=""):
        return True


@pytest.fixture
def two_machines(monkeypatch: pytest.MonkeyPatch) -> dict:
    backends = {
        "multicam": FakeBackend("multicam", "Multicam", can_load=True),
        "woodtron": FakeBackend("woodtron", "Woodtron", can_load=False),
    }
    monkeypatch.setattr(app_module, "_backends", backends)
    return backends


def test_machine_list_reports_both_machines(client: TestClient, two_machines) -> None:
    body = client.get("/api/m").json()
    assert [m["key"] for m in body["machines"]] == ["multicam", "woodtron"]
    assert body["machines"][0]["capabilities"]["load"] is True
    assert body["machines"][1]["capabilities"]["load"] is False


def test_machine_list_survives_a_backend_that_throws(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreachable machine must not blank out the switcher."""
    class Broken(FakeBackend):
        async def status(self):
            raise RuntimeError("network on fire")

    monkeypatch.setattr(app_module, "_backends", {
        "good": FakeBackend("good", "Good"),
        "bad": Broken("bad", "Bad"),
    })
    body = client.get("/api/m").json()
    assert len(body["machines"]) == 2
    bad = next(m for m in body["machines"] if m["key"] == "bad")
    assert bad["online"] is False


def test_files_payload_carries_capabilities_and_separator(
    client: TestClient, two_machines
) -> None:
    body = client.get("/api/m/woodtron/files").json()
    assert body["machine"] == "woodtron"
    assert body["sep"] == "/"
    assert body["capabilities"]["load"] is False
    assert body["files"][0]["name"] == "a.nc"


def test_unknown_machine_is_a_404(client: TestClient, two_machines) -> None:
    assert client.get("/api/m/nope/files").status_code == 404


def test_load_is_refused_on_a_machine_that_cannot_load(
    client: TestClient, two_machines
) -> None:
    """The Woodtron has no 'current job' — the route must say so, not pretend."""
    r = client.post("/api/m/woodtron/load/a.nc")
    assert r.status_code == 409
    assert "current job" in r.json()["detail"]

    assert client.post("/api/m/multicam/load/a.nc").status_code == 200


def test_delete_passes_the_folder_and_is_dir_through(
    client: TestClient, two_machines
) -> None:
    """Deleting while inside a subfolder must target that folder, not the root."""
    r = client.delete("/api/m/woodtron/files/old.nc", params={"path": "/NcTemp"})
    assert r.status_code == 200
    assert two_machines["woodtron"].deleted == [("old.nc", "/NcTemp", False)]

    client.delete("/api/m/woodtron/files/Sub", params={"path": "/", "is_dir": "true"})
    assert two_machines["woodtron"].deleted[-1] == ("Sub", "/", True)


def test_upload_still_rejects_non_nc_files(client: TestClient, two_machines) -> None:
    r = client.post(
        "/api/m/woodtron/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 422


def test_cleanup_ghost_files_removes_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs = tmp_path / "Jobs"
    jobs.mkdir()
    (jobs / "part1.nc").write_text("G0", encoding="utf-8")
    (jobs / "._part1.nc").write_text("junk", encoding="utf-8")
    monkeypatch.setattr(app_module, "GHOST_CLEANUP_DIR", str(jobs))
    assert app_module._cleanup_ghost_files() == 1
    assert not (jobs / "._part1.nc").exists()


# ── machines save/load round-trip ───────────────────────────────────────────


def test_machines_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = tmp_path / "machines.json"
    monkeypatch.setattr(app_module, "_MACHINES_FILE", store)

    assert app_module._load_machines() == []

    app_module._save_machine("192.168.0.45", "TRIDENT")
    assert app_module._load_machines() == [{"ip": "192.168.0.45", "name": "TRIDENT"}]


def test_machines_dedupe_keeps_most_recent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "machines.json"
    monkeypatch.setattr(app_module, "_MACHINES_FILE", store)

    app_module._save_machine("192.168.0.45", "TRIDENT")
    app_module._save_machine("192.168.0.50", "OTHER")
    app_module._save_machine("192.168.0.45", "TRIDENT-RENAMED")

    machines = app_module._load_machines()
    assert [m["ip"] for m in machines] == ["192.168.0.45", "192.168.0.50"]
    assert machines[0]["name"] == "TRIDENT-RENAMED"


def test_machines_capped_at_ten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "machines.json"
    monkeypatch.setattr(app_module, "_MACHINES_FILE", store)

    for i in range(15):
        app_module._save_machine(f"192.168.0.{i}", f"M{i}")

    assert len(app_module._load_machines()) == 10
