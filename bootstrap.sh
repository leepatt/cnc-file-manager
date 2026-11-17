#!/usr/bin/env bash
# Local dev bootstrap (Mac/Linux) — run once after cloning.
#
# The workshop PC does NOT use this: on Windows, bootstrap.bat builds the
# environment in C:\CNCFileManager\venv and INSTALL-BACKGROUND-SERVICE.bat runs
# it as an always-on service. This is for working on the code elsewhere.
set -euo pipefail
python3 -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
echo "Done."
echo
echo "Run it:    .venv/bin/uvicorn file_manager.app:app --reload --port 8097"
echo "Tests:     .venv/bin/pytest tests/ -v"
echo "Typecheck: .venv/bin/pyright"
