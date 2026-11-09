"""AC 09: offline update with integrity check, migration, health check and automatic rollback."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import text

from app.db import make_engine
from app.ops import backup as backup_ops
from app.ops.schema import MIGRATIONS, current_version, migrate
from app.ops.updater import apply_update, current_release, sha256_file, verify_package


def build_pkg(dst: Path, version: str, files: dict[str, str]) -> Path:
    dst.mkdir(parents=True)
    manifest = {"version": version, "files": {}}
    for rel, content in files.items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        manifest["files"][rel] = sha256_file(p)
    (dst / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dst


def _add_index(conn):
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_member_name ON member(name)"))


def _broken_migration(conn):
    conn.execute(text("CREATE TABLE half_done (id INTEGER)"))  # DDL succeeds ...
    raise RuntimeError("migration 6 exploded")  # ... then the step fails


def _tables(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def setup_install(tmp_path, rt, catalog):
    rt.engine.dispose()
    root = tmp_path / "install"
    (root / "releases").mkdir(parents=True)
    (root / "current.json").write_text(json.dumps({"version": "1.0.0"}))
    return root


def factory(settings):
    return lambda: make_engine(settings.db_url)


def healthy(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM item")).scalar_one() > 0


def test_good_update_migrates_and_switches_pointer(tmp_path, rt, settings, catalog):
    root = setup_install(tmp_path, rt, catalog)
    pkg = build_pkg(tmp_path / "pkg110", "1.1.0", {"app/code.py": "print('v1.1')", "README.txt": "notes"})
    plan = MIGRATIONS + [(4, "member name index", _add_index)]
    result = apply_update(
        root, pkg, db_path=settings.db_path, engine_factory=factory(settings), health_check=healthy, migrations=plan
    )
    assert result.ok and not result.rolled_back and result.migrations_applied == [4]
    assert current_release(root) == "1.1.0"
    assert (root / "releases" / "1.1.0" / "app" / "code.py").exists()
    engine = make_engine(settings.db_url)
    assert current_version(engine) == 4
    engine.dispose()


def test_tampered_package_is_rejected_before_anything_changes(tmp_path, rt, settings, catalog):
    root = setup_install(tmp_path, rt, catalog)
    before = backup_ops.fingerprint(settings.db_path)
    pkg = build_pkg(tmp_path / "pkg", "1.1.0", {"app/code.py": "print('ok')"})
    (pkg / "app" / "code.py").write_text("print('malicious')")
    result = apply_update(root, pkg, db_path=settings.db_path, engine_factory=factory(settings), health_check=healthy)
    assert not result.ok and "integrity" in result.detail
    assert current_release(root) == "1.0.0"
    assert backup_ops.diff_fingerprints(before, backup_ops.fingerprint(settings.db_path)) == []
    assert not (root / "releases" / "1.1.0").exists()


def test_unexpected_extra_file_is_rejected(tmp_path):
    pkg = build_pkg(tmp_path / "pkg", "1.1.0", {"a.txt": "a"})
    (pkg / "evil.sh").write_text("rm -rf /")
    try:
        verify_package(pkg)
    except Exception as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("extra file must fail verification")


def test_failed_health_check_restores_previous_state(tmp_path, rt, settings, catalog):
    root = setup_install(tmp_path, rt, catalog)
    before = backup_ops.fingerprint(settings.db_path)
    pkg = build_pkg(tmp_path / "pkg", "1.2.0", {"app/code.py": "x"})

    def unhealthy(_engine):
        raise RuntimeError("smoke test failed")

    plan = MIGRATIONS + [(4, "member name index", _add_index)]
    result = apply_update(
        root, pkg, db_path=settings.db_path, engine_factory=factory(settings), health_check=unhealthy, migrations=plan
    )
    assert not result.ok and result.rolled_back and "smoke test failed" in result.detail
    assert current_release(root) == "1.0.0"  # old release still active
    assert not (root / "releases" / "1.2.0").exists()
    assert backup_ops.diff_fingerprints(before, backup_ops.fingerprint(settings.db_path)) == []
    engine = make_engine(settings.db_url)
    assert current_version(engine) == 3  # migration 4 was undone with the restore
    engine.dispose()


def test_failed_migration_with_partial_ddl_is_undone_by_restore(tmp_path, rt, settings, catalog):
    """SQLite DDL via pysqlite can auto-commit, so the backup is the real safety net."""
    root = setup_install(tmp_path, rt, catalog)
    before = backup_ops.fingerprint(settings.db_path)
    pkg = build_pkg(tmp_path / "pkg", "1.3.0", {"app/code.py": "x"})
    plan = MIGRATIONS + [(4, "ok step", _add_index), (5, "broken step", _broken_migration)]
    result = apply_update(
        root, pkg, db_path=settings.db_path, engine_factory=factory(settings), health_check=healthy, migrations=plan
    )
    assert result.rolled_back and "exploded" in result.detail
    assert "half_done" not in _tables(settings.db_path)
    assert backup_ops.diff_fingerprints(before, backup_ops.fingerprint(settings.db_path)) == []
    assert current_release(root) == "1.0.0"


def test_rollback_leaves_data_readable_and_app_startable(tmp_path, rt, settings, catalog):
    root = setup_install(tmp_path, rt, catalog)
    pkg = build_pkg(tmp_path / "pkg", "1.4.0", {"f": "x"})
    apply_update(
        root,
        pkg,
        db_path=settings.db_path,
        engine_factory=factory(settings),
        health_check=lambda e: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    from app.bootstrap import build_runtime

    rt2 = build_runtime(settings)
    with rt2.factory() as s:
        assert s.execute(text("SELECT COUNT(*) FROM item")).scalar_one() == len(catalog)
    rt2.engine.dispose()


def test_migrations_are_idempotent_and_ordered(rt):
    assert migrate(rt.engine) == []
    assert current_version(rt.engine) == MIGRATIONS[-1][0]


def test_search_index_is_maintained_by_triggers(rt, catalog):
    from app.services.catalog import search_catalog

    with rt.factory() as s:
        hits = search_catalog(s, "Title of ACC00")
        assert {h["copies"][0]["accession_no"] for h in hits} >= {"ACC001", "ACC009"}
        assert [h["copies"][0]["accession_no"] for h in search_catalog(s, "acc010")] == ["ACC010"]
        assert search_catalog(s, '"; DROP TABLE bib; --') == []  # punctuation stripped, no injection
        assert search_catalog(s, "   ") == []
