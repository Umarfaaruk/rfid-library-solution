"""Launcher. Starts the active release with the installation's own interpreter and data folder.

python run.py serve [--port 8000]      run the API
python run.py <any app.cli command>    e.g. health, backup, import FILE --dry-run
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    version = json.loads((ROOT / "current.json").read_text(encoding="utf-8"))["version"]
    py = ROOT / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {**os.environ, "RFIDLIB_DATA_DIR": str(ROOT / "data")}
    return subprocess.call([str(py), "-m", "app.cli", *sys.argv[1:]], cwd=ROOT / "releases" / version, env=env)


if __name__ == "__main__":
    sys.exit(main())
