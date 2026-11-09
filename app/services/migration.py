"""Spreadsheet to catalogue migration (FR 10, AC 01).

Pipeline: read -> normalise -> validate -> dedupe -> (dry run stops here) -> load one batch -> reconcile.
Every source row ends in exactly one bucket: loaded, rejected or duplicate.
Rollback removes exactly the rows carrying the batch id.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Settings
from app.db import utcnow
from app.errors import DomainError, NotFound
from app.models import Bib, ImportBatch, Item, Loan, StgImport
from app.ops import backup as backup_ops

COLUMNS = ["Accession No", "Title", "Author", "ISBN", "Publisher", "Year", "Location", "Category", "Barcode"]
_WS = re.compile(r"\s+")


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield dict rows from .csv or .xlsx, all values as stripped strings."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                yield {k.strip(): (v or "").strip() for k, v in row.items() if k}
    elif suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        rows = wb.active.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
        for values in rows:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            yield {h: ("" if v is None else str(v).strip()) for h, v in zip(header, values, strict=False) if h}
        wb.close()
    else:
        raise DomainError("UNSUPPORTED_FILE", f"Unsupported file type: {suffix}")


def norm_text(value: str) -> str:
    return _WS.sub(" ", value or "").strip()


def norm_isbn(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def isbn_valid(isbn: str) -> bool:
    if len(isbn) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", isbn):
            return False
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn))
        return total % 10 == 0
    return False


def validate_row(raw: dict[str, str], *, max_year: int) -> tuple[dict | None, str | None]:
    """Return (clean, None) or (None, reason)."""
    acc = norm_text(raw.get("Accession No", ""))
    title = norm_text(raw.get("Title", ""))
    barcode = norm_text(raw.get("Barcode", ""))
    location = norm_text(raw.get("Location", ""))
    if not acc:
        return None, "missing_accession_no"
    if not title:
        return None, "missing_title"
    if not barcode:
        return None, "missing_barcode"
    if not location:
        return None, "missing_location"
    category = norm_text(raw.get("Category", "")).lower() or "lending"
    if category not in {"lending", "reference"}:
        return None, "invalid_category"
    isbn = norm_isbn(raw.get("ISBN", ""))
    if isbn and not isbn_valid(isbn):
        return None, "invalid_isbn"
    year_raw = norm_text(raw.get("Year", ""))
    year: int | None = None
    if year_raw:
        if not re.fullmatch(r"\d{4}", year_raw) or not (1400 <= int(year_raw) <= max_year):
            return None, "invalid_year"
        year = int(year_raw)
    return {
        "accession_no": acc,
        "barcode": barcode,
        "title": title,
        "author": norm_text(raw.get("Author", "")) or None,
        "isbn": isbn or None,
        "publisher": norm_text(raw.get("Publisher", "")) or None,
        "year": year,
        "location": location,
        "is_reference": category == "reference",
    }, None


def _bib_key(clean: dict) -> tuple:
    if clean["isbn"]:
        return ("isbn", clean["isbn"])
    return ("ta", clean["title"].lower(), (clean["author"] or "").lower(), clean["year"])


def run_import(
    session: Session,
    settings: Settings,
    path: Path,
    *,
    dry_run: bool,
    actor: str = "system",
    now: datetime | None = None,
) -> dict:
    """Stage, validate and (unless dry_run) load. Returns the reconciliation report."""
    now = now or utcnow()
    if not dry_run and settings.db_path.exists():
        # NFR 01: a restore point exists before any real load touches the database.
        backup_ops.create_backup(settings.db_path, settings.backups_dir, label="pre-import")
    batch = ImportBatch(source_name=path.name, started_at=now, status="staging", actor=actor)
    session.add(batch)
    session.flush()

    existing_acc = set(session.scalars(select(Item.accession_no)))
    existing_bar = set(session.scalars(select(Item.barcode)))
    seen_acc: set[str] = set()
    seen_bar: set[str] = set()
    staged: list[dict] = []
    valid: list[dict] = []
    max_year = now.year + 1

    for row_no, raw in enumerate(read_rows(path), start=2):  # row 1 is the header
        clean, reason = validate_row(raw, max_year=max_year)
        status = "valid"
        if clean is None:
            status = "rejected"
        elif clean["accession_no"] in seen_acc or clean["accession_no"] in existing_acc:
            status, reason = (
                "duplicate",
                ("duplicate_in_catalogue" if clean["accession_no"] in existing_acc else "duplicate_in_file"),
            )
        elif clean["barcode"] in seen_bar or clean["barcode"] in existing_bar:
            status, reason = "rejected", "barcode_conflict"
        if status == "valid":
            seen_acc.add(clean["accession_no"])
            seen_bar.add(clean["barcode"])
            valid.append(clean)
        staged.append({"batch_id": batch.id, "row_no": row_no, "raw": raw, "status": status, "reason": reason})

    if staged:
        session.execute(insert(StgImport), staged)

    loaded = 0
    if not dry_run and valid:
        loaded = _load(session, batch.id, valid)

    counts = Counter(s["status"] for s in staged)
    rejected_by = Counter(s["reason"] for s in staged if s["status"] == "rejected")
    duplicate_by = Counter(s["reason"] for s in staged if s["status"] == "duplicate")
    source_rows = len(staged)
    items_in_db = session.scalar(select(func.count()).select_from(Item).where(Item.source_batch_id == batch.id))
    expected_loaded = len(valid) if not dry_run else 0
    report = {
        "batch_id": batch.id,
        "source": path.name,
        "mode": "dry_run" if dry_run else "load",
        "source_rows": source_rows,
        "valid": counts["valid"],
        "loaded": loaded,
        "rejected": counts["rejected"],
        "duplicates": counts["duplicate"],
        "rejected_by_reason": dict(rejected_by),
        "duplicates_by_reason": dict(duplicate_by),
        "items_in_catalogue_from_batch": items_in_db,
        "balanced": (
            source_rows == counts["valid"] + counts["rejected"] + counts["duplicate"]
            and items_in_db == expected_loaded
            and (dry_run or loaded == counts["valid"])
        ),
    }
    batch.status = "dry_run" if dry_run else "loaded"
    batch.finished_at = utcnow()
    batch.report = report
    audit(
        session,
        actor,
        "import.dry_run" if dry_run else "import.load",
        "import_batch",
        batch.id,
        {k: report[k] for k in ("source", "source_rows", "loaded", "rejected", "duplicates")},
        now=now,
    )
    _write_reports(settings, batch.id, report, staged)
    return report


def _load(session: Session, batch_id: int, valid: list[dict]) -> int:
    next_bib = (session.scalar(select(func.max(Bib.id))) or 0) + 1
    bib_ids: dict[tuple, int] = {}
    bibs: list[dict] = []
    items: list[dict] = []
    for clean in valid:
        key = _bib_key(clean)
        if key not in bib_ids:
            bib_ids[key] = next_bib
            bibs.append(
                {
                    "id": next_bib,
                    "title": clean["title"],
                    "author": clean["author"],
                    "isbn": clean["isbn"],
                    "publisher": clean["publisher"],
                    "year": clean["year"],
                    "source_batch_id": batch_id,
                }
            )
            next_bib += 1
        items.append(
            {
                "bib_id": bib_ids[key],
                "accession_no": clean["accession_no"],
                "barcode": clean["barcode"],
                "location": clean["location"],
                "is_reference": clean["is_reference"],
                "status": "available",
                "source_batch_id": batch_id,
            }
        )
    session.execute(insert(Bib), bibs)
    session.execute(insert(Item), items)
    return len(items)


def _write_reports(settings: Settings, batch_id: int, report: dict, staged: list[dict]) -> None:
    out = settings.reports_dir / f"batch_{batch_id}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "reconciliation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (out / "error_log.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["row_no", "status", "reason", "accession_no", "title"])
        for s in staged:
            if s["status"] != "valid":
                writer.writerow(
                    [s["row_no"], s["status"], s["reason"], s["raw"].get("Accession No", ""), s["raw"].get("Title", "")]
                )


def rollback_import(session: Session, batch_id: int, *, actor: str = "system") -> dict:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFound("BATCH_NOT_FOUND", f"No import batch {batch_id}")
    if batch.status != "loaded":
        raise DomainError(
            "BATCH_NOT_ROLLBACKABLE", f"Batch {batch_id} is {batch.status}, only loaded batches can be rolled back"
        )
    in_use = session.scalar(
        select(func.count())
        .select_from(Loan)
        .join(Item, Loan.item_id == Item.id)
        .where(Item.source_batch_id == batch_id)
    )
    if in_use:
        raise DomainError("BATCH_IN_USE", f"{in_use} loans reference items from this batch; return or clear them first")
    from app.models import RfidTag

    item_ids = select(Item.id).where(Item.source_batch_id == batch_id)
    tags = session.execute(delete(RfidTag).where(RfidTag.item_id.in_(item_ids))).rowcount
    items = session.execute(delete(Item).where(Item.source_batch_id == batch_id)).rowcount
    bibs = session.execute(delete(Bib).where(Bib.source_batch_id == batch_id)).rowcount
    batch.status = "rolled_back"
    batch.finished_at = utcnow()
    result = {"batch_id": batch_id, "items_removed": items, "bibs_removed": bibs, "tags_removed": tags}
    audit(session, actor, "import.rollback", "import_batch", batch_id, result)
    return result
