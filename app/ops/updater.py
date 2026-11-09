"""Offline update with automatic rollback (FR 12, AC 09).

A package is a folder with manifest.json: {"version": "...", "files": {relative_path: sha256}}.
Update sequence: verify hashes -> back up the database -> stage the new release -> migrate ->
health check -> switch the 'current' pointer. Any failure restores the backup and leaves the
previous release active.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine

from app.ops import backup as backup_ops
from app.ops.schema import Migration, migrate

log = logging.getLogger(__name__)


class PackageError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_package(pkg_dir: Path) -> dict:
    manifest_path = pkg_dir / "manifest.json"
    if not manifest_path.exists():
        raise PackageError("manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for rel, expected in manifest["files"].items():
        target = pkg_dir / rel
        if not target.is_file() or sha256_file(target) != expected:
            bad.append(rel)
    on_disk = {
        p.relative_to(pkg_dir).as_posix() for p in pkg_dir.rglob("*") if p.is_file() and p.name != "manifest.json"
    }
    extra = sorted(on_disk - set(manifest["files"]))
    if bad or extra:
        raise PackageError(f"integrity check failed: modified/missing={bad[:5]} unexpected={extra[:5]}")
    return manifest


@dataclass
class UpdateResult:
    ok: bool
    from_version: str | None
    to_version: str
    rolled_back: bool = False
    detail: str = ""
    backup: str | None = None
    migrations_applied: list[int] = field(default_factory=list)


def current_release(install_root: Path) -> str | None:
    pointer = install_root / "current.json"
    return json.loads(pointer.read_text())["version"] if pointer.exists() else None


def _write_pointer(install_root: Path, version: str) -> None:
    tmp = install_root / "current.json.tmp"
    tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
    tmp.replace(install_root / "current.json")  # atomic switch


def apply_update(
    install_root: Path,
    pkg_dir: Path,
    *,
    db_path: Path,
    engine_factory: Callable[[], Engine],
    health_check: Callable[[Engine], None],
    migrations: list[Migration] | None = None,
) -> UpdateResult:
    """engine_factory must build a fresh engine on the database file each call."""
    previous = current_release(install_root)
    try:
        manifest = verify_package(pkg_dir)
    except PackageError as exc:
        return UpdateResult(False, previous, "?", detail=str(exc))
    version = manifest["version"]
    backups = install_root / "backups"
    backup_file = backup_ops.create_backup(db_path, backups, label=f"pre-{version}")
    release_dir = install_root / "releases" / version
    result = UpdateResult(False, previous, version, backup=backup_file.name)
    engine = engine_factory()
    try:
        if release_dir.exists():
            shutil.rmtree(release_dir)
        shutil.copytree(pkg_dir, release_dir)
        result.migrations_applied = migrate(engine, migrations)
        health_check(engine)
        _write_pointer(install_root, version)
        result.ok = True
        return result
    except Exception as exc:
        log.error("update to %s failed: %s; restoring backup", version, exc)
        engine.dispose()
        backup_ops.restore_backup(backup_file, db_path)
        shutil.rmtree(release_dir, ignore_errors=True)
        result.rolled_back = True
        result.detail = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        engine.dispose()
