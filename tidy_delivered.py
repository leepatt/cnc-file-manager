"""One-off tidy: file away jobs that were delivered before archiving existed.

Auto-send now moves a file into `Sent` as soon as it reaches the machine. Files
delivered by an *earlier* build were left in the main folder, so they sit there
looking like outstanding work when they have already been cut.

This clears that backlog once. It is deliberately not automatic, and it is
deliberately cautious about which files it will touch:

  - The watcher's own record must say the file was delivered at exactly its
    current size and timestamp. Edit a file after it was sent and it is left
    alone — it is genuinely waiting to go again.
  - The delivery must also appear in the log. On first run the watcher marks
    pre-existing files as "already sent" without sending them (so a new PC
    doesn't flood the machines); those files were never actually delivered and
    must not be filed away as though they were. The log is what tells the two
    apart.
  - Nothing is overwritten: a name already in `Sent` gets a " (2)" suffix.
  - Nothing is deleted. Every file is MOVED, and only after it is safely in the
    archive.

Run it with no arguments to see what it would do. Nothing moves until you pass
`--apply`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from file_manager.watchers import unique_target  # noqa: E402

STATE_DIR = Path(r"C:\CNCFileManager\state")
LOG_DIR = Path(r"C:\CNCFileManager\logs")
ARCHIVE = "Sent"

WATCHERS = [
    ("multicam", "watch_state.json", "FM_WATCH_DIR"),
    ("woodtron", "watch_state_woodtron.json", "FM_WOODTRON_DIR"),
]


def read_env(env_path: Path) -> dict:
    settings = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        settings[key.strip()] = value.strip()
    return settings


def delivered_names(key: str) -> set[str]:
    """Every file this watcher's log records as actually sent."""
    names: set[str] = set()
    pattern = re.compile(rf"{key}: sent (.+?) \(\d+ bytes\)")
    for log in sorted(LOG_DIR.glob("file-manager.log*")):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(pattern.findall(text))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually move the files (default: just report)")
    args = parser.parse_args()

    env = read_env(Path(__file__).parent / ".env")
    total = 0
    moved = 0

    for key, statefile, dir_setting in WATCHERS:
        folder = env.get(dir_setting, "")
        if not folder:
            continue
        folder_path = Path(folder)
        state_path = STATE_DIR / statefile
        if not folder_path.is_dir() or not state_path.is_file():
            print(f"\n{key}: folder or state file missing — skipped")
            continue

        sent = json.loads(state_path.read_text(encoding="utf-8")).get("sent", {})
        confirmed = delivered_names(key)
        archive = folder_path / ARCHIVE

        print(f"\n=== {key}  ({folder_path}) ===")
        candidates = []
        for path in sorted(folder_path.glob("*.nc")):
            st = path.stat()
            sig = [st.st_size, int(st.st_mtime)]
            if sent.get(path.name) != sig:
                print(f"  keep   {path.name}  (not delivered at this version)")
                continue
            if path.name not in confirmed:
                print(f"  keep   {path.name}  (no delivery in the log — never actually sent)")
                continue
            candidates.append(path)

        total += len(candidates)
        for path in candidates:
            if not args.apply:
                print(f"  WOULD MOVE  {path.name}  ->  {ARCHIVE}\\")
                continue
            archive.mkdir(parents=True, exist_ok=True)
            target = unique_target(archive, path.name)
            path.replace(target)
            moved += 1
            suffix = "" if target.name == path.name else f" (as {target.name})"
            print(f"  moved  {path.name}  ->  {ARCHIVE}\\{suffix}")

    print()
    if args.apply:
        print(f"Done. Moved {moved} file(s) into '{ARCHIVE}'.")
    else:
        print(f"{total} file(s) would be moved into '{ARCHIVE}'.")
        print("Nothing has changed. Re-run with --apply to do it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
