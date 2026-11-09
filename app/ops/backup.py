"""Consistent backups, restore, and content fingerprints used as restore evidence (AC 09, AC 10)."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.models import Base


def create_backup(db_path: Path, backups_dir: Path, *, label: str = "manual") -> Path:
    """Online-consistent copy using SQLite's backup API (safe while the app is running)."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups_dir / f"{label}-{stamp}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def restore_backup(backup_file: Path, db_path: Path) -> None:
    """Replace the live database file with a backup. The caller must dispose engines first."""
    if not backup_file.exists():
        raise FileNotFoundError(backup_file)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    shutil.copyfile(backup_file, db_path)


def fingerprint(db_path: Path) -> dict[str, dict]:
    """Per-table row count and SHA-256 over all rows in primary-key order."""
    conn = sqlite3.connect(db_path)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict[str, dict] = {}
        for table in sorted(Base.metadata.tables):
            if table not in existing:
                continue
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'):  # noqa: S608 - names come from our own metadata
                digest.update(repr(row).encode("utf-8"))
                count += 1
            out[table] = {"rows": count, "sha256": digest.hexdigest()}
        return out
    finally:
        conn.close()


def diff_fingerprints(a: dict, b: dict) -> list[str]:
    """Names of tables whose content differs."""
    return [t for t in sorted(set(a) | set(b)) if a.get(t) != b.get(t)]
