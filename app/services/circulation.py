"""Circulation rules (FR 03, FR 04). Rules run in a fixed order and return a coded reason.

Order for checkout: member exists -> not blocked -> under fine limit -> item exists ->
not reference -> available -> under loan limit. Then one transaction writes loan, event and audit,
and the tag is desensitised. If the device fails the whole action fails (fail closed).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Settings
from app.db import utcnow
from app.errors import DomainError
from app.models import Item, Loan, LoanEvent, Member, RfidTag
from app.rfid.devices import DeviceHub
from app.services.catalog import resolve_item, resolve_member
from app.services.notifications import notify_member


def apply_security(session: Session, hub: DeviceHub | None, item: Item, secured: bool) -> None:
    """Set the security state on the item's active tag, on the device first, then in the database."""
    tag = session.scalar(select(RfidTag).where(RfidTag.item_id == item.id, RfidTag.status == "active"))
    if tag is None:
        return
    if hub is not None:
        hub.reader.set_security(tag.uid, secured)
    tag.secured = secured


def _open_loan(session: Session, item_id: int) -> Loan | None:
    return session.scalar(select(Loan).where(Loan.item_id == item_id, Loan.returned_at.is_(None)))


def check_member_can_borrow(member: Member, settings: Settings) -> None:
    if member.blocked:
        raise DomainError("MEMBER_BLOCKED", f"Member {member.member_no} is blocked")
    if member.fines_due >= settings.fine_limit:
        raise DomainError(
            "FINE_LIMIT_EXCEEDED",
            f"Outstanding fines {member.fines_due:.2f} reach the limit {settings.fine_limit:.2f}",
        )


def checkout(
    session: Session,
    settings: Settings,
    *,
    member_key: str,
    item_key: str,
    actor: str,
    source: str = "ui",
    hub: DeviceHub | None = None,
    now: datetime | None = None,
) -> Loan:
    now = now or utcnow()
    member = resolve_member(session, member_key)
    check_member_can_borrow(member, settings)
    item = resolve_item(session, item_key)
    if item.is_reference:
        raise DomainError("REFERENCE_ITEM", f"{item.accession_no} is a reference item and does not circulate")
    if item.status != "available" or _open_loan(session, item.id):
        raise DomainError("ITEM_UNAVAILABLE", f"{item.accession_no} is not available")
    active = session.scalar(
        select(func.count()).select_from(Loan).where(Loan.member_id == member.id, Loan.returned_at.is_(None))
    )
    if active >= settings.max_loans:
        raise DomainError("LOAN_LIMIT_REACHED", f"Member already holds {active} items (limit {settings.max_loans})")

    loan = Loan(item_id=item.id, member_id=member.id, out_at=now, due_at=now + timedelta(days=settings.loan_days))
    session.add(loan)
    session.flush()
    item.status = "on_loan"
    session.add(LoanEvent(loan_id=loan.id, kind="checkout", at=now, source=source, actor=actor))
    apply_security(session, hub, item, secured=False)
    audit(
        session,
        actor,
        "loan.checkout",
        "loan",
        loan.id,
        {"member": member.member_no, "item": item.accession_no, "source": source, "due": loan.due_at.isoformat()},
        now=now,
    )
    notify_member(session, member, "Checkout receipt", f"You borrowed {item.accession_no}. Due {loan.due_at:%Y-%m-%d}.")
    return loan


def checkin(
    session: Session,
    settings: Settings,
    *,
    item_key: str,
    actor: str,
    source: str = "ui",
    hub: DeviceHub | None = None,
    now: datetime | None = None,
) -> Loan:
    now = now or utcnow()
    item = resolve_item(session, item_key)
    loan = _open_loan(session, item.id)
    if loan is None:
        raise DomainError("NOT_ON_LOAN", f"{item.accession_no} is not on loan")
    overdue_days = max(0, (now.date() - loan.due_at.date()).days)
    fine = round(overdue_days * settings.fine_per_day, 2)
    loan.returned_at = now
    loan.fine_charged = fine
    item.status = "available"
    member = session.get(Member, loan.member_id)
    if fine:
        member.fines_due = round(member.fines_due + fine, 2)
    session.add(LoanEvent(loan_id=loan.id, kind="checkin", at=now, source=source, actor=actor))
    apply_security(session, hub, item, secured=True)
    audit(
        session,
        actor,
        "loan.checkin",
        "loan",
        loan.id,
        {
            "member": member.member_no,
            "item": item.accession_no,
            "overdue_days": overdue_days,
            "fine": fine,
            "source": source,
        },
        now=now,
    )
    if fine:
        notify_member(session, member, "Overdue fine added", f"A fine of {fine:.2f} was added for {item.accession_no}.")
    return loan


def renew(
    session: Session,
    settings: Settings,
    *,
    item_key: str,
    actor: str,
    source: str = "ui",
    now: datetime | None = None,
) -> Loan:
    now = now or utcnow()
    item = resolve_item(session, item_key)
    loan = _open_loan(session, item.id)
    if loan is None:
        raise DomainError("NOT_ON_LOAN", f"{item.accession_no} is not on loan")
    member = session.get(Member, loan.member_id)
    check_member_can_borrow(member, settings)
    if loan.due_at < now:
        raise DomainError("OVERDUE_RENEWAL", "Overdue items must be returned before they can be borrowed again")
    if loan.renewals >= settings.max_renewals:
        raise DomainError("RENEWAL_LIMIT_REACHED", f"Maximum {settings.max_renewals} renewals reached")
    loan.due_at = loan.due_at + timedelta(days=settings.loan_days)
    loan.renewals += 1
    session.add(LoanEvent(loan_id=loan.id, kind="renew", at=now, source=source, actor=actor))
    audit(
        session,
        actor,
        "loan.renew",
        "loan",
        loan.id,
        {"member": member.member_no, "item": item.accession_no, "new_due": loan.due_at.isoformat(), "source": source},
        now=now,
    )
    return loan


def pay_fine(session: Session, *, member_key: str, amount: float, actor: str) -> Member:
    if amount <= 0:
        raise DomainError("INVALID_AMOUNT", "Payment must be positive")
    member = resolve_member(session, member_key)
    member.fines_due = round(max(0.0, member.fines_due - amount), 2)
    audit(session, actor, "fine.pay", "member", member.member_no, {"amount": amount, "remaining": member.fines_due})
    return member


def member_loans(session: Session, member_key: str) -> list[dict]:
    member = resolve_member(session, member_key)
    rows = session.execute(
        select(Loan, Item)
        .join(Item, Loan.item_id == Item.id)
        .where(Loan.member_id == member.id, Loan.returned_at.is_(None))
        .order_by(Loan.due_at)
    ).all()
    now = utcnow()
    return [
        {
            "accession_no": i.accession_no,
            "title": i.bib.title,
            "due_at": loan.due_at.isoformat(),
            "renewals": loan.renewals,
            "overdue": loan.due_at < now,
        }
        for loan, i in rows
    ]
