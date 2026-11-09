"""Offline installer. Standard library only. Run from the extracted package folder:

    python install.py <target_dir>

Verifies every file against manifest.json, creates a private virtual environment, installs the
bundled wheels with --no-index (no network access is attempted), initialises the database and
records the active release.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify() -> dict:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    bad = [
        rel for rel, digest in manifest["files"].items() if not (HERE / rel).is_file() or sha256(HERE / rel) != digest
    ]
    if bad:
        sys.exit(f"Package integrity check FAILED for: {bad[:5]}")
    print(f"integrity ok: {len(manifest['files'])} files")
    return manifest


def venv_python(root: Path) -> Path:
    return root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python install.py <target_dir>")
    target = Path(sys.argv[1]).resolve()
    manifest = verify()
    version = manifest["version"]
    if (target / "current.json").exists():
        sys.exit(f"{target} already contains an installation; use update.py instead")
    target.mkdir(parents=True, exist_ok=True)
    release = target / "releases" / version
    shutil.copytree(HERE / "app", release / "app")
    shutil.copy2(HERE / "run.py", target / "run.py")
    shutil.copy2(HERE / "update.py", target / "update.py")
    venv.EnvBuilder(with_pip=True, clear=False).create(target / "venv")
    py = str(venv_python(target))
    subprocess.run(
        [
            py,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(HERE / "wheels"),
            "--disable-pip-version-check",
            "-r",
            str(HERE / "requirements.lock"),
        ],
        check=True,
    )
    (target / "data").mkdir(exist_ok=True)
    env = {**os.environ, "RFIDLIB_DATA_DIR": str(target / "data")}
    subprocess.run([py, "-m", "app.cli", "init-db"], cwd=release, env=env, check=True)
    (target / "current.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    print(f"installed {version} to {target}\nstart with: python {target / 'run.py'} serve")


if __name__ == "__main__":
    main()
