"""Build the offline installer package.

    python scripts/build_offline_package.py --version 1.0.0 --out dist
    python scripts/build_offline_package.py --version 1.0.0 --out dist --windows

Produces dist/rfid-library-<version>-offline/ (and a .zip plus .sha256). The folder holds the app
source, every dependency as a wheel, requirements.lock, an SBOM with licences, installer, launcher,
updater and a manifest with a SHA-256 for every file. Needs network ONCE, on the build machine only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DEPS = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "pydantic",
    "pyjwt",
    "openpyxl",
    "defusedxml",
    "pillow",
    "python-multipart",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wheel_meta(whl: Path) -> dict:
    with zipfile.ZipFile(whl) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        msg = Parser().parsestr(z.read(name).decode("utf-8", "replace"))
    lic = msg.get("License-Expression") or msg.get("License") or ""
    if not lic or len(lic) > 80:
        classifiers = [c.split("::")[-1].strip() for c in msg.get_all("Classifier", []) if c.startswith("License ::")]
        lic = "; ".join(classifiers) or "see wheel metadata"
    return {"name": msg["Name"], "version": msg["Version"], "license": lic, "file": whl.name}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--windows", action="store_true", help="download Windows x64 wheels (CPython 3.12)")
    ap.add_argument("--src", type=Path, default=ROOT, help="source tree to package (default: this repository)")
    args = ap.parse_args()

    src = args.src
    pkg = args.out / f"rfid-library-{args.version}-offline"
    if pkg.exists():
        shutil.rmtree(pkg)
    (pkg / "wheels").mkdir(parents=True)

    shutil.copytree(src / "app", pkg / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("install.py", "run.py", "update.py"):
        shutil.copy2(src / "packaging" / name, pkg / name)
    docs = pkg / "docs"
    docs.mkdir()
    for f in (src / "docs").glob("*.md"):
        shutil.copy2(f, docs / f.name)
    shutil.copy2(src / "README.md", pkg / "README.md")

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-d",
        str(pkg / "wheels"),
        "--disable-pip-version-check",
        "--only-binary=:all:",
        *RUNTIME_DEPS,
    ]
    if args.windows:
        cmd += ["--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp"]
    subprocess.run(cmd, check=True)

    sbom = [wheel_meta(w) for w in sorted((pkg / "wheels").glob("*.whl"))]
    (pkg / "SBOM.json").write_text(
        json.dumps({"app": "rfid-library", "version": args.version, "components": sbom}, indent=2)
    )
    lock = [f"{re.sub('_', '-', c['name']).lower()}=={c['version']}" for c in sbom]
    (pkg / "requirements.lock").write_text("\n".join(lock) + "\n", encoding="utf-8")

    files = {p.relative_to(pkg).as_posix(): sha256(p) for p in sorted(pkg.rglob("*")) if p.is_file()}
    manifest = {
        "version": args.version,
        "target": "windows-x64-cp312" if args.windows else "build-host",
        "files": files,
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    archive = shutil.make_archive(str(pkg), "zip", root_dir=args.out, base_dir=pkg.name)
    Path(archive + ".sha256").write_text(f"{sha256(Path(archive))}  {Path(archive).name}\n")
    print(f"built {archive} ({Path(archive).stat().st_size / 1e6:.1f} MB), {len(sbom)} wheels, {len(files)} files")


if __name__ == "__main__":
    main()
