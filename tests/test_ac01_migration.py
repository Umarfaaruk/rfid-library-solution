"""AC 01: import ~20,000 spreadsheet rows, validate, dedupe, load, reconcile, roll back."""

from __future__ import annotations

import csv
import json
import time

from sqlalchemy import func, select

from app.models import Bib, Item
from app.services import migration
from app.services.migration import isbn_valid, run_import, validate_row
from scripts.generate_synthetic_data import build
from tests.conftest import write_catalog

BIG = 20_000


def _write_big(tmp_path):
    data, truth = build(19_901, seed=7)  # 19,901 base rows + 99 injected duplicates = 20,000
    path = tmp_path / "big.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(migration.COLUMNS)
        w.writerows(data)
    return path, truth


def test_dry_run_writes_no_catalogue_rows(rt, tmp_path):
    path, truth = _write_big(tmp_path)
    with rt.factory() as s:
        report = run_import(s, rt.settings, path, dry_run=True)
        s.commit()
        assert report["mode"] == "dry_run" and report["loaded"] == 0
        assert report["valid"] == truth["valid"]
        assert s.scalar(select(func.count()).select_from(Item)) == 0
        assert s.scalar(select(func.count()).select_from(Bib)) == 0


def test_full_load_matches_ground_truth_and_reconciles(rt, tmp_path):
    path, truth = _write_big(tmp_path)
    with rt.factory() as s:
        t0 = time.perf_counter()
        report = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
        elapsed = time.perf_counter() - t0
        assert report["source_rows"] == truth["source_rows"] == BIG
        assert report["rejected"] == truth["rejected"]
        assert report["duplicates"] == truth["duplicates"]
        assert report["loaded"] == truth["valid"]
        assert report["rejected_by_reason"] == {k: v for k, v in truth["reasons"].items() if k != "duplicate_in_file"}
        assert report["balanced"] is True
        assert s.scalar(select(func.count()).select_from(Item)) == truth["valid"]
        # Whitespace-padded titles were normalised, not rejected.
        assert s.scalar(select(func.count()).select_from(Bib).where(Bib.title.like(" %"))) == 0
    assert elapsed < 30, f"import took {elapsed:.1f}s"


def test_reports_are_written(rt, tmp_path):
    path, _ = _write_big(tmp_path)
    with rt.factory() as s:
        report = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
    out = rt.settings.reports_dir / f"batch_{report['batch_id']}"
    assert json.loads((out / "reconciliation.json").read_text())["balanced"] is True
    lines = (out / "error_log.csv").read_text().splitlines()
    assert len(lines) == 1 + report["rejected"] + report["duplicates"]


def test_rollback_removes_only_this_batch(rt, tmp_path, catalog):
    path, truth = _write_big(tmp_path)
    # Big file reuses ACC000001.. so it would collide with ACC001; prefix-free check below.
    with rt.factory() as s:
        before = s.scalar(select(func.count()).select_from(Item))
        report = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Item)) == before + truth["valid"]
        result = migration.rollback_import(s, report["batch_id"])
        s.commit()
        assert result["items_removed"] == truth["valid"]
        assert s.scalar(select(func.count()).select_from(Item)) == before
        assert s.scalar(select(func.count()).select_from(Bib)) == before  # catalogue fixture: one bib per item


def test_reimport_marks_everything_duplicate(rt, tmp_path):
    path = write_catalog(
        tmp_path / "c.csv",
        [
            {"Accession No": "X1", "Title": "T1", "Location": "L", "Barcode": "B1"},
            {"Accession No": "X2", "Title": "T2", "Location": "L", "Barcode": "B2"},
        ],
    )
    with rt.factory() as s:
        first = run_import(s, rt.settings, path, dry_run=False)
        second = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
    assert first["loaded"] == 2
    assert second["loaded"] == 0 and second["duplicates"] == 2 and second["balanced"] is True


def test_barcode_conflict_is_rejected_not_loaded(rt, tmp_path):
    path = write_catalog(
        tmp_path / "c.csv",
        [
            {"Accession No": "X1", "Title": "T1", "Location": "L", "Barcode": "SAME"},
            {"Accession No": "X2", "Title": "T2", "Location": "L", "Barcode": "SAME"},
        ],
    )
    with rt.factory() as s:
        report = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
    assert report["loaded"] == 1
    assert report["rejected_by_reason"] == {"barcode_conflict": 1}


def test_xlsx_input_supported(rt, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(migration.COLUMNS)
    ws.append(["A1", "Sheet Title", "", "", "", 2001, "MAIN", "Lending", "BX1"])
    ws.append([None] * 9)  # blank row is skipped
    path = tmp_path / "c.xlsx"
    wb.save(path)
    with rt.factory() as s:
        report = run_import(s, rt.settings, path, dry_run=False)
        s.commit()
    assert report["source_rows"] == 1 and report["loaded"] == 1


def test_validation_rules():
    ok = {"Accession No": "A", "Title": "T", "Location": "L", "Barcode": "B"}
    assert validate_row(ok, max_year=2030)[1] is None
    assert validate_row({**ok, "Year": "1200"}, max_year=2030)[1] == "invalid_year"
    assert validate_row({**ok, "Year": "2031"}, max_year=2030)[1] == "invalid_year"
    assert validate_row({**ok, "Category": "x"}, max_year=2030)[1] == "invalid_category"
    assert validate_row({**ok, "ISBN": "978-0-306-40615-7"}, max_year=2030)[1] is None
    assert validate_row({**ok, "ISBN": "978-0-306-40615-8"}, max_year=2030)[1] == "invalid_isbn"
    assert isbn_valid("080442957X") and not isbn_valid("080442957Y")


def test_rollback_refused_when_items_are_on_loan(rt, tmp_path):
    from app.errors import DomainError
    from app.services import circulation

    path = write_catalog(tmp_path / "c.csv", [{"Accession No": "X1", "Title": "T", "Location": "L", "Barcode": "B"}])
    with rt.factory() as s:
        report = run_import(s, rt.settings, path, dry_run=False)
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="X1", actor="t")
        s.commit()
        try:
            migration.rollback_import(s, report["batch_id"])
        except DomainError as err:
            assert err.code == "BATCH_IN_USE"
        else:
            raise AssertionError("rollback should be refused")
