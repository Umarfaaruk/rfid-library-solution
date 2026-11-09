"""AC 04: reference items, blocked members, fine limits, loan limits, renewals, overdue fines."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.errors import DomainError
from app.models import Member, RfidTag
from app.services import circulation

T0 = datetime(2026, 11, 2, 10, 0, 0)


def _code(fn, *a, **k) -> str:
    with pytest.raises(DomainError) as e:
        fn(*a, **k)
    return e.value.code


def test_reference_item_cannot_be_borrowed(rt, catalog):
    with rt.factory() as s:
        assert (
            _code(circulation.checkout, s, rt.settings, member_key="M0001", item_key="ACC011", actor="t")
            == "REFERENCE_ITEM"
        )


def test_blocked_member_cannot_borrow_or_renew(rt, catalog):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", now=T0)
        s.query(Member).filter_by(member_no="M0001").one().blocked = True
        s.flush()
        assert (
            _code(circulation.checkout, s, rt.settings, member_key="M0001", item_key="ACC002", actor="t")
            == "MEMBER_BLOCKED"
        )
        assert _code(circulation.renew, s, rt.settings, item_key="ACC001", actor="t", now=T0) == "MEMBER_BLOCKED"


def test_fine_limit_blocks_checkout_until_paid_down(rt, catalog):
    with rt.factory() as s:
        s.query(Member).filter_by(member_no="M0002").one().fines_due = rt.settings.fine_limit
        s.flush()
        assert (
            _code(circulation.checkout, s, rt.settings, member_key="M0002", item_key="ACC001", actor="t")
            == "FINE_LIMIT_EXCEEDED"
        )
        circulation.pay_fine(s, member_key="M0002", amount=10, actor="t")
        assert circulation.checkout(s, rt.settings, member_key="M0002", item_key="ACC001", actor="t").id


def test_loan_limit(rt, catalog):
    with rt.factory() as s:
        for n in range(1, rt.settings.max_loans + 1):
            circulation.checkout(s, rt.settings, member_key="M0001", item_key=f"ACC{n:03d}", actor="t")
        code = _code(circulation.checkout, s, rt.settings, member_key="M0001", item_key="ACC007", actor="t")
        assert code == "LOAN_LIMIT_REACHED"


def test_item_cannot_be_borrowed_twice(rt, catalog):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t")
        assert (
            _code(circulation.checkout, s, rt.settings, member_key="M0002", item_key="ACC001", actor="t")
            == "ITEM_UNAVAILABLE"
        )


def test_overdue_checkin_charges_fine_and_notifies(rt, catalog):
    with rt.factory() as s:
        loan = circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", now=T0)
        late = loan.due_at + timedelta(days=6)
        done = circulation.checkin(s, rt.settings, item_key="ACC001", actor="t", now=late)
        assert done.fine_charged == 6 * rt.settings.fine_per_day == 12.0
        assert s.query(Member).filter_by(member_no="M0001").one().fines_due == 12.0
        from app.models import Notification

        subjects = [n.subject for n in s.query(Notification)]
        assert "Overdue fine added" in subjects and "Checkout receipt" in subjects


def test_on_time_return_has_no_fine(rt, catalog):
    with rt.factory() as s:
        loan = circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", now=T0)
        done = circulation.checkin(s, rt.settings, item_key="ACC001", actor="t", now=loan.due_at)
        assert done.fine_charged == 0


def test_renewals_extend_due_date_up_to_the_limit(rt, catalog):
    with rt.factory() as s:
        loan = circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", now=T0)
        first_due = loan.due_at
        for _ in range(rt.settings.max_renewals):
            circulation.renew(s, rt.settings, item_key="ACC001", actor="t", now=T0 + timedelta(days=1))
        assert loan.renewals == rt.settings.max_renewals
        assert loan.due_at == first_due + timedelta(days=rt.settings.loan_days * rt.settings.max_renewals)
        assert _code(circulation.renew, s, rt.settings, item_key="ACC001", actor="t", now=T0) == "RENEWAL_LIMIT_REACHED"


def test_overdue_item_cannot_be_renewed(rt, catalog):
    with rt.factory() as s:
        loan = circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", now=T0)
        assert (
            _code(circulation.renew, s, rt.settings, item_key="ACC001", actor="t", now=loan.due_at + timedelta(days=1))
            == "OVERDUE_RENEWAL"
        )


def test_checkout_and_checkin_flip_security_state(rt, hub, tagged):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", hub=hub)
        tag = s.query(RfidTag).filter_by(uid="TAG-ACC001").one()
        assert tag.secured is False
        assert hub.reader.memory["TAG-ACC001"].afi == rt.settings.afi_unsecured == 0x07
        circulation.checkin(s, rt.settings, item_key="ACC001", actor="t", hub=hub)
        assert tag.secured is True
        assert hub.reader.memory["TAG-ACC001"].afi == rt.settings.afi_secured


def test_device_fault_blocks_checkout_fail_closed(app, rt, hub, tagged, librarian, client):
    hub.reader.fail_next(1)
    r = client.post("/circulation/checkout", json={"member": "M0001", "item": "ACC001"}, headers=librarian)
    assert r.status_code == 502 and r.json()["code"] == "DEVICE_FAULT"
    # The failed attempt left no loan behind and the item is still available.
    info = client.get("/catalog/items/ACC001", headers=librarian).json()
    assert info["status"] == "available" and info["tag"]["secured"] is True
    ok = client.post("/circulation/checkout", json={"member": "M0001", "item": "ACC001"}, headers=librarian)
    assert ok.status_code == 200


def test_every_transaction_is_audited(rt, catalog):
    from app.models import AuditLog

    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="alice")
        circulation.renew(s, rt.settings, item_key="ACC001", actor="alice")
        circulation.checkin(s, rt.settings, item_key="ACC001", actor="alice")
        s.commit()
        actions = [a.action for a in s.query(AuditLog).order_by(AuditLog.id)]
        assert actions[-3:] == ["loan.checkout", "loan.renew", "loan.checkin"]
