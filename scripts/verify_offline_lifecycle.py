"""End-to-end proof of the offline lifecycle (AC 09, AC 10), run for real:

 1. build 1.0.0, 1.1.0 (adds a migration) and 1.2.0 (deliberately broken migration)
 2. install 1.0.0 with the network unreachable (--no-index and a dead proxy)
 3. import the 20,000-row synthetic spreadsheet through the installed launcher
 4. start the API from the install and call it over HTTP
 5. update to 1.1.0 (must succeed)
 6. update to the broken 1.2.0 (must roll back; database fingerprint must be unchanged)
 7. tamper with a package file (must be rejected)

Writes docs/evidence/offline_lifecycle_report.json.   Usage: python scripts/verify_offline_lifecycle.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.ops.backup import fingerprint  # noqa: E402

DEAD_NET = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def build(work: Path, version: str, patch: str | None) -> Path:
    src = work / f"src-{version}"
    shutil.copytree(
        ROOT,
        src,
        ignore=shutil.ignore_patterns(
            ".git", "dist", "data", "__pycache__", ".pytest_cache", ".ruff_cache", "*.egg-info", ".coverage"
        ),
    )
    init = src / "app" / "__init__.py"
    init.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{version}"', init.read_text()))
    if patch:
        with (src / "app" / "ops" / "schema.py").open("a", encoding="utf-8") as fh:
            fh.write(patch)
    out = work / "dist"
    r = run(
        [
            sys.executable,
            ROOT / "scripts" / "build_offline_package.py",
            "--version",
            version,
            "--out",
            out,
            "--src",
            src,
        ]
    )
    assert r.returncode == 0, r.stderr[-800:]
    with zipfile.ZipFile(out / f"rfid-library-{version}-offline.zip") as z:
        z.extractall(work / f"pkg-{version}")
    return work / f"pkg-{version}" / f"rfid-library-{version}-offline"


PATCH_OK = """
def _v4_member_name_index(conn):
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_member_name ON member(name)"))


MIGRATIONS.append((4, "member name index", _v4_member_name_index))
LATEST = MIGRATIONS[-1][0]
"""
PATCH_BROKEN = (
    PATCH_OK
    + """

def _v5_broken(conn):
    conn.execute(text("CREATE TABLE half_done (id INTEGER)"))
    raise RuntimeError("release 1.2.0 migration failed midway")


MIGRATIONS.append((5, "broken step", _v5_broken))
LATEST = MIGRATIONS[-1][0]
"""
)


def tables(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def main() -> None:
    report: dict = {"steps": []}

    def step(name: str, ok: bool, **detail) -> None:
        report["steps"].append({"step": name, "ok": ok, **detail})
        print(("PASS " if ok else "FAIL ") + name, detail if detail else "")
        if not ok:
            finish(1)

    def finish(code: int) -> None:
        out = ROOT / "docs" / "evidence"
        out.mkdir(parents=True, exist_ok=True)
        (out / "offline_lifecycle_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        sys.exit(code)

    work = Path(tempfile.mkdtemp(prefix="rfid-lifecycle-"))
    try:
        p100 = build(work, "1.0.0", None)
        p110 = build(work, "1.1.0", PATCH_OK)
        p120 = build(work, "1.2.0", PATCH_BROKEN)
        step("built three packages", True, versions=["1.0.0", "1.1.0", "1.2.0"])

        install = work / "site"
        env = {**os.environ, **DEAD_NET}
        r = run([sys.executable, p100 / "install.py", install], env=env)
        step("install 1.0.0 with network unreachable", r.returncode == 0, tail=r.stdout.strip().splitlines()[-1:])
        launcher = [sys.executable, install / "run.py"]
        h = run([*launcher, "health"])
        step("health after install", h.returncode == 0 and '"status": "ok"' in h.stdout, out=h.stdout.strip())
        run([*launcher, "seed-demo"])

        csv_file = work / "synthetic.csv"
        run([sys.executable, ROOT / "scripts" / "generate_synthetic_data.py", "--rows", "19901", "--out", csv_file])
        truth = json.loads(csv_file.with_suffix(".truth.json").read_text())
        t0 = time.perf_counter()
        imp = run([*launcher, "import", csv_file])
        took = time.perf_counter() - t0
        rep = json.loads(imp.stdout)
        step(
            "import 20,000 rows via installed launcher",
            imp.returncode == 0 and rep["balanced"] and rep["loaded"] == truth["valid"] and rep["source_rows"] == 20000,
            source_rows=rep["source_rows"],
            loaded=rep["loaded"],
            rejected=rep["rejected"],
            duplicates=rep["duplicates"],
            seconds=round(took, 1),
        )

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen(
            [str(c) for c in [*launcher, "serve", "--port", port]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            body = None
            for _ in range(60):
                try:
                    body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read())
                    break
                except Exception:
                    time.sleep(0.5)
            step("API serves /health from the installed copy", bool(body) and body["status"] == "ok", health=body)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/auth/login",
                method="POST",
                data=json.dumps({"username": "librarian", "password": "Libra#2026"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            token = json.loads(urllib.request.urlopen(req, timeout=5).read())["token"]
            sr = urllib.request.Request(
                f"http://127.0.0.1:{port}/catalog/search?q=Physics&limit=3",
                headers={"Authorization": f"Bearer {token}"},
            )
            hits = json.loads(urllib.request.urlopen(sr, timeout=5).read())["results"]
            step(
                "authenticated catalogue search over HTTP",
                len(hits) == 3,
                first_title=hits[0]["title"] if hits else None,
            )
        finally:
            server.terminate()
            server.wait(timeout=10)

        db = install / "data" / "library.db"
        before = fingerprint(db)
        up = run([sys.executable, install / "update.py", p110], env=env)
        ok_result = json.loads(up.stdout.strip().splitlines()[-1]) if up.stdout.strip() else {}
        ptr = json.loads((install / "current.json").read_text())["version"]
        step(
            "update to 1.1.0 succeeds",
            up.returncode == 0 and ptr == "1.1.0" and ok_result.get("migrations_applied") == [4],
            pointer=ptr,
            migrations=ok_result.get("migrations_applied"),
            rc=up.returncode,
            log=(up.stderr + up.stdout)[-700:] if up.returncode else "",
        )

        mid = fingerprint(db)
        bad = run([sys.executable, install / "update.py", p120], env=env)
        bad_result = json.loads(bad.stdout.strip().splitlines()[-1]) if bad.stdout.strip() else {}
        after = fingerprint(db)
        step(
            "broken 1.2.0 update rolls back",
            bad.returncode == 1
            and bad_result.get("rolled_back") is True
            and json.loads((install / "current.json").read_text())["version"] == "1.1.0",
            detail=bad_result.get("detail"),
        )
        step(
            "database identical after failed update",
            mid == after and "half_done" not in tables(db),
            tables_compared=len(after),
        )
        step(
            "data preserved across the whole lifecycle",
            after["item"]["rows"] == before["item"]["rows"] == truth["valid"],
            items=after["item"]["rows"],
        )

        (p110 / "app" / "main.py").write_text("# tampered\n")
        t = run([sys.executable, install / "update.py", p110], env=env)
        step("tampered package rejected", t.returncode == 2 and "integrity" in (t.stderr + t.stdout).lower())
        finish(0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
