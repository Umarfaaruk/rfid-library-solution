"""Dashboard metrics and CSV export (FR 08). Read-only queries, filterable by date and location."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Bib, Device, GateEvent, Item, Loan, LoanEvent, Member, RfidTag, User


def dashboard(
    session: Session, *, start: datetime | None = None, end: datetime | None = None, location: str | None = None
) -> dict:
    now = utcnow()

    def item_filter(stmt):
        return stmt.where(Item.location == location) if location else stmt

    items_total = session.scalar(item_filter(select(func.count()).select_from(Item)))
    tagged = session.scalar(
        item_filter(
            select(func.count(distinct(Item.id))).select_from(Item).join(RfidTag, RfidTag.item_id == Item.id)
        ).where(RfidTag.status == "active")
    )
    ev = (
        select(LoanEvent.kind, func.count())
        .join(Loan, LoanEvent.loan_id == Loan.id)
        .join(Item, Loan.item_id == Item.id)
    )
    if start:
        ev = ev.where(LoanEvent.at >= start)
    if end:
        ev = ev.where(LoanEvent.at <= end)
    if location:
        ev = ev.where(Item.location == location)
    by_kind = dict(session.execute(ev.group_by(LoanEvent.kind)).all())

    active = item_filter(select(func.count()).select_from(Loan).join(Item, Loan.item_id == Item.id)).where(
        Loan.returned_at.is_(None)
    )
    gate = select(GateEvent.kind, func.coalesce(func.sum(GateEvent.count), 0)).group_by(GateEvent.kind)
    if start:
        gate = gate.where(GateEvent.at >= start)
    if end:
        gate = gate.where(GateEvent.at <= end)
    gate_by = {k: int(v) for k, v in session.execute(gate).all()}
    alarms = session.scalar(
        select(func.count())
        .select_from(GateEvent)
        .where(
            GateEvent.kind == "alarm",
            *([GateEvent.at >= start] if start else []),
            *([GateEvent.at <= end] if end else []),
        )
    )
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "filters": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "location": location,
        },
        "catalogue": {
            "titles": session.scalar(select(func.count()).select_from(Bib)),
            "items": items_total,
            "items_tagged": tagged,
            "tagged_percent": round(100 * tagged / items_total, 1) if items_total else 0.0,
        },
        "members": session.scalar(select(func.count()).select_from(Member)),
        "operators": session.scalar(select(func.count()).select_from(User).where(User.active.is_(True))),
        "rfid_clients": session.scalar(select(func.count()).select_from(Device)),
        "circulation": {
            "checkouts": by_kind.get("checkout", 0),
            "checkins": by_kind.get("checkin", 0),
            "renewals": by_kind.get("renew", 0),
            "active_loans": session.scalar(active),
            "overdue_now": session.scalar(active.where(Loan.due_at < now)),
        },
        "fines_outstanding": round(session.scalar(select(func.coalesce(func.sum(Member.fines_due), 0.0))), 2),
        "gate": {"alarms": alarms, "passes": gate_by.get("pass", 0), "footfall": gate_by.get("footfall", 0)},
    }


def circulation_csv(
    session: Session, *, start: datetime | None = None, end: datetime | None = None, location: str | None = None
) -> str:
    stmt = (
        select(
            Loan.id,
            Member.member_no,
            Item.accession_no,
            Bib.title,
            Item.location,
            Loan.out_at,
            Loan.due_at,
            Loan.returned_at,
            Loan.renewals,
            Loan.fine_charged,
        )
        .join(Item, Loan.item_id == Item.id)
        .join(Bib, Item.bib_id == Bib.id)
        .join(Member, Loan.member_id == Member.id)
        .order_by(Loan.id)
    )
    if start:
        stmt = stmt.where(Loan.out_at >= start)
    if end:
        stmt = stmt.where(Loan.out_at <= end)
    if location:
        stmt = stmt.where(Item.location == location)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "loan_id",
            "member_no",
            "accession_no",
            "title",
            "location",
            "out_at",
            "due_at",
            "returned_at",
            "renewals",
            "fine_charged",
        ]
    )
    for row in session.execute(stmt):
        w.writerow(["" if v is None else (v.isoformat() if isinstance(v, datetime) else v) for v in row])
    return buf.getvalue()
