"""Offline updater. Run from the installation folder:

    python update.py <extracted_new_package_dir>

Order: verify new package -> install its wheels (no network) -> back up the database -> stage release ->
run migrations -> health check -> switch the active release. Any failure after the backup restores the
database and keeps the previous release active.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: python update.py <new_package_dir>")
    pkg = Path(sys.argv[1]).resolve()
    py = ROOT / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Run the new package's own updater logic under the installation's interpreter.
    # -B: never write .pyc files into the package, or its strict integrity check would flag them.
    code = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(sys.argv[1])))\n"
        "from app.config import Settings\n"
        "from app.db import make_engine\n"
        "from app.ops.updater import apply_update, verify_package\n"
        "from sqlalchemy import text\n"
        "from app.ops.schema import LATEST, current_version\n"
        "root, pkg = Path(sys.argv[2]), Path(sys.argv[1])\n"
        "s = Settings.from_env()\n"
        "def health(engine):\n"
        "    with engine.connect() as c:\n"
        "        c.execute(text('SELECT 1'))\n"
        "    v = current_version(engine)\n"
        "    if v != LATEST:\n"
        "        raise RuntimeError(f'schema {v} != expected {LATEST}')\n"
        "r = apply_update(root, pkg, db_path=s.db_path,\n"
        "                 engine_factory=lambda: make_engine(s.db_url), health_check=health)\n"
        "print(json.dumps(r.__dict__, default=str))\n"
        "sys.exit(0 if r.ok else 1)\n"
    )
    verify = subprocess.run(
        [
            str(py),
            "-B",
            "-c",
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "from pathlib import Path;from app.ops.updater import verify_package;"
            "print(verify_package(Path(sys.argv[1]))['version'])",
            str(pkg),
        ],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        print(verify.stderr.strip() or verify.stdout.strip(), file=sys.stderr)
        return 2
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(pkg / "wheels"),
            "--disable-pip-version-check",
            "-r",
            str(pkg / "requirements.lock"),
        ],
        check=True,
    )
    env = {**os.environ, "RFIDLIB_DATA_DIR": str(ROOT / "data")}
    return subprocess.call([str(py), "-B", "-c", code, str(pkg), str(ROOT)], env=env)


if __name__ == "__main__":
    sys.exit(main())
