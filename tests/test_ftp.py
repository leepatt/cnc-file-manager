"""FTP listing parser and path handling for the Woodtron.

The Woodtron's Windows CE server has no MLSD, no SIZE and no MDTM, so *every*
fact about a file comes from parsing human-readable `LIST` output. That makes
this parser the single point where a Woodtron file listing can silently go
wrong, which is why the samples below are real lines captured from the shop's
machine rather than invented ones.
"""
from __future__ import annotations

from file_manager.ftp import normalize_ftp_path, parse_list_line

# Captured verbatim from 192.168.0.200 on 2026-08-26.
REAL_LINES = [
    "08-19-26  14:07                26371 959 NEST (2 on 1)_S1 Q2.nc",
    "05-01-24  08:18       <DIR>          AutoLabel",
    "08-13-26  15:05       <DIR>          20260813_1505",
    "08-19-26  16:15       <DIR>          NcTemp",
    "08-20-26  14:19                 3150 CLIFTONS JIG V2.nc",
    "08-24-26  12:22               135811 Clifton For_EXT 02 V2 Q42.nc",
]


def test_parses_every_real_line() -> None:
    parsed = [parse_list_line(line) for line in REAL_LINES]
    assert all(p is not None for p in parsed)


def test_reads_a_file_entry() -> None:
    e = parse_list_line("08-20-26  14:19                 3150 CLIFTONS JIG V2.nc")
    assert e is not None
    assert e.name == "CLIFTONS JIG V2.nc"
    assert e.size == 3150
    assert e.is_dir is False
    assert e.modified == "2026-08-20 14:19"


def test_reads_a_directory_entry() -> None:
    e = parse_list_line("05-01-24  08:18       <DIR>          AutoLabel")
    assert e is not None
    assert e.name == "AutoLabel"
    assert e.is_dir is True
    assert e.size == 0
    assert e.modified == "2024-05-01 08:18"


def test_keeps_spaces_brackets_and_parens_in_job_names() -> None:
    """Real job names are nothing like tidy identifiers."""
    e = parse_list_line("08-19-26  14:07                26371 959 NEST (2 on 1)_S1 Q2.nc")
    assert e is not None
    assert e.name == "959 NEST (2 on 1)_S1 Q2.nc"
    assert e.size == 26371


def test_unix_format_is_understood_too() -> None:
    """Defensive: if this machine is ever replaced by a normal FTP server."""
    e = parse_list_line("-rw-r--r--   1 owner group        26371 Aug 19 14:07 part.nc")
    assert e is not None and e.name == "part.nc" and e.size == 26371 and not e.is_dir
    d = parse_list_line("drwxr-xr-x   2 owner group         4096 Aug 19 14:07 JOBS")
    assert d is not None and d.is_dir is True


def test_junk_lines_are_skipped_not_fatal() -> None:
    """One odd line must not blank out an entire folder listing."""
    assert parse_list_line("") is None
    assert parse_list_line("total 42") is None
    assert parse_list_line("some server banner") is None
    assert parse_list_line("05-01-24  08:18       <DIR>          .") is None
    assert parse_list_line("05-01-24  08:18       <DIR>          ..") is None


# ── path handling ───────────────────────────────────────────────────────────


def test_paths_normalize_to_absolute() -> None:
    assert normalize_ftp_path("", "/") == "/"
    assert normalize_ftp_path("/", "/") == "/"
    assert normalize_ftp_path("NcTemp", "/") == "/NcTemp"
    assert normalize_ftp_path("/NcTemp/", "/") == "/NcTemp"
    assert normalize_ftp_path("\\NcTemp", "/") == "/NcTemp"  # a stray Windows slash


def test_dot_dot_cannot_escape_the_root() -> None:
    assert normalize_ftp_path("/NcTemp/..", "/") == "/"
    assert normalize_ftp_path("/../../etc", "/") == "/etc"  # resolved, still under root
    assert normalize_ftp_path("/a/b/../c", "/") == "/a/c"


def test_a_non_root_root_is_enforced() -> None:
    """If the Woodtron is ever configured to serve a subfolder, stay inside it."""
    assert normalize_ftp_path("/jobs/sub", "/jobs") == "/jobs/sub"
    assert normalize_ftp_path("/jobs/..", "/jobs") == "/jobs"
    assert normalize_ftp_path("/elsewhere", "/jobs") == "/jobs"
