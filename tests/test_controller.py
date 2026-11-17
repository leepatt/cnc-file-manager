"""Unit tests for the pure parsing/validation surface of ControllerClient.

These cover the bits that don't touch the network: the `Get Files` reply
parser, filename validation, and controller-path joining.
"""
from __future__ import annotations

import pytest

from file_manager.controller import (
    DEFAULT_DIR,
    ControllerClient,
    ControllerError,
    RemoteFile,
    clamp_path_to_root,
    resolve_upload_dir,
)


def make_client() -> ControllerClient:
    return ControllerClient(ip="192.168.0.45", name="TRIDENT")


# ── _validate_name ──────────────────────────────────────────────────────────


def test_validate_name_accepts_normal_nc_name() -> None:
    ControllerClient._validate_name("part1.nc")  # no exception


def test_validate_name_rejects_leading_underscore() -> None:
    with pytest.raises(ControllerError):
        ControllerClient._validate_name("_hidden.nc")


# ── _join ───────────────────────────────────────────────────────────────────


def test_join_uses_backslash_and_strips_trailing_sep() -> None:
    c = make_client()
    c.cwd = r"\hard disk2\release\jobs\\"
    assert c._join("part1.nc") == r"\hard disk2\release\jobs\part1.nc"


# ── _parse_file_list (collection sizes 0, 1, N) ─────────────────────────────


def test_parse_file_list_empty_dir_returns_nothing() -> None:
    c = make_client()
    # Only the echoed directory field, no entries.
    assert c._parse_file_list(DEFAULT_DIR) == []


def test_parse_file_list_single_file() -> None:
    c = make_client()
    c.cwd = DEFAULT_DIR
    reply = f"{DEFAULT_DIR}~part1.nc"
    files = c._parse_file_list(reply)
    assert files == [
        RemoteFile(
            name="part1.nc",
            path=rf"{DEFAULT_DIR}\part1.nc",
            is_dir=False,
        )
    ]


def test_parse_file_list_mixed_folder_and_files() -> None:
    c = make_client()
    c.cwd = DEFAULT_DIR
    reply = (
        f"{DEFAULT_DIR}"
        rf"~{DEFAULT_DIR}\JONIK [folder 3 files]"
        "~part1.nc~part2.nc"
    )
    files = c._parse_file_list(reply)
    assert [(f.name, f.is_dir) for f in files] == [
        ("JONIK", True),
        ("part1.nc", False),
        ("part2.nc", False),
    ]
    folder = files[0]
    assert folder.path == rf"{DEFAULT_DIR}\JONIK"


def test_parse_file_list_skips_blank_entries() -> None:
    c = make_client()
    c.cwd = DEFAULT_DIR
    reply = f"{DEFAULT_DIR}~~part1.nc~~"
    files = c._parse_file_list(reply)
    assert [f.name for f in files] == ["part1.nc"]


# ── _decode_download (wire ~-stream → file bytes) ───────────────────────────


def test_decode_download_single_line() -> None:
    assert ControllerClient._decode_download(b"G0 X0 Y0~EOF\r\n") == b"G0 X0 Y0\r\n"


def test_decode_download_multi_line() -> None:
    raw = b"(HEADER)~G0 X0~G1 Y10~M30~EOF\r\n"
    assert ControllerClient._decode_download(raw) == b"(HEADER)\r\nG0 X0\r\nG1 Y10\r\nM30\r\n"


def test_decode_download_ignores_trailing_status_after_eof() -> None:
    # The controller appends status noise ("Bad", "NA") after ~EOF; it must not
    # leak into the file.
    raw = b"a~b~EOF\r\nBad\r\nNA\r\n"
    assert ControllerClient._decode_download(raw) == b"a\r\nb\r\n"


def test_decode_download_without_eof_marker_falls_back() -> None:
    assert ControllerClient._decode_download(b"a~b\r\n") == b"a\r\nb\r\n"


# ── clamp_path_to_root ──────────────────────────────────────────────────────


def test_clamp_path_to_root_allows_jobs_and_subfolders() -> None:
    assert clamp_path_to_root(DEFAULT_DIR) == DEFAULT_DIR
    assert clamp_path_to_root(r"\hard disk2\release\jobs\PROD") == r"\hard disk2\release\jobs\PROD"
    assert clamp_path_to_root(r"\hard disk2\release\Jobs\JONIK") == r"\hard disk2\release\Jobs\JONIK"


def test_clamp_path_to_root_blocks_parent_paths() -> None:
    assert clamp_path_to_root(r"\hard disk2\release") == DEFAULT_DIR
    assert clamp_path_to_root(r"\hard disk2") == DEFAULT_DIR
    assert clamp_path_to_root(r"\hard disk2\release\job") == DEFAULT_DIR


# ── resolve_upload_dir ───────────────────────────────────────────────────────


def test_resolve_upload_dir_empty_returns_root() -> None:
    assert resolve_upload_dir("") == DEFAULT_DIR
    assert resolve_upload_dir("   ") == DEFAULT_DIR


def test_resolve_upload_dir_joins_subfolder() -> None:
    assert resolve_upload_dir("WEB-GENERATED/boxmaker") == DEFAULT_DIR + r"\WEB-GENERATED\boxmaker"
    assert resolve_upload_dir(r"WEB-GENERATED\boxmaker") == DEFAULT_DIR + r"\WEB-GENERATED\boxmaker"


def test_resolve_upload_dir_strips_surrounding_slashes() -> None:
    assert resolve_upload_dir("/WEB-GENERATED/boxmaker/") == DEFAULT_DIR + r"\WEB-GENERATED\boxmaker"


@pytest.mark.parametrize(
    "subdir",
    ["..", "../escape", "WEB-GENERATED/..", "WEB-GENERATED/../../escape", "C:/escape", r"C:\escape"],
)
def test_resolve_upload_dir_rejects_traversal_and_drive_letters(subdir: str) -> None:
    with pytest.raises(ValueError):
        resolve_upload_dir(subdir)
