"""AC 10: a failed or unwanted change can be undone by restoring a backup, with proof of identical data."""

from __future__ import annotations

from app.bootstrap import build_runtime
from app.ops import backup as backup_ops
from app.services.migration import run_import
from tests.conftest import make_rows, write_catalog


def test_restore_returns_database_to_exact_prior_state(rt, settings, catalog, tmp_path):
    baseline = backup_ops.fingerprint(settings.db_path)
    backup_file = backup_ops.create_backup(settings.db_path, settings.backups_dir, label="before-change")

    # An unwanted change: another import adds rows and audit entries.
    extra = write_catalog(
        tmp_path / "extra.csv", make_rows([("NEW001", "MAIN-A1", False), ("NEW002", "MAIN-A1", False)])
    )
    with rt.factory() as s:
        run_import(s, settings, extra, dry_run=False, actor="test")
        s.commit()
    changed = backup_ops.fingerprint(settings.db_path)
    assert backup_ops.diff_fingerprints(baseline, changed), "the change must be detectable"
    assert changed["item"]["rows"] == baseline["item"]["rows"] + 2

    rt.engine.dispose()
    backup_ops.restore_backup(backup_file, settings.db_path)

    restored = backup_ops.fingerprint(settings.db_path)
    assert backup_ops.diff_fingerprints(baseline, restored) == []
    rt2 = build_runtime(settings)  # app starts normally on the restored file
    rt2.engine.dispose()


def test_backup_is_consistent_while_connections_are_open(rt, settings, catalog):
    with rt.factory() as s:  # keep a session open while backing up
        s.connection()
        path = backup_ops.create_backup(settings.db_path, settings.backups_dir)
    assert backup_ops.fingerprint(path)["item"]["rows"] == len(catalog)


def test_fingerprint_changes_when_one_value_changes(rt, settings, catalog):
    from sqlalchemy import update

    from app.models import Item

    before = backup_ops.fingerprint(settings.db_path)
    with rt.factory() as s:
        s.execute(update(Item).where(Item.accession_no == "ACC001").values(location="ELSEWHERE"))
        s.commit()
    after = backup_ops.fingerprint(settings.db_path)
    assert backup_ops.diff_fingerprints(before, after) == ["item"]


def test_real_import_takes_a_restore_point_first_but_dry_run_does_not(rt, settings, tmp_path):
    src = write_catalog(tmp_path / "c.csv", make_rows([("P1", "MAIN-A1", False)]))
    with rt.factory() as s:
        run_import(s, settings, src, dry_run=True, actor="test")
        s.commit()
    assert not list(settings.backups_dir.glob("*pre-import*")) if settings.backups_dir.exists() else True
    with rt.factory() as s:
        run_import(s, settings, src, dry_run=False, actor="test")
        s.commit()
    assert len(list(settings.backups_dir.glob("*pre-import*"))) == 1
