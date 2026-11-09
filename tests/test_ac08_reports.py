"""AC 08: dashboard figures and CSV export are correct and filterable by date and location."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from app.db import utcnow
from app.services import circulation, reports


def _activity(rt, hub):
    now = utcnow()
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC001", actor="t", hub=hub, now=now)
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC002", actor="t", hub=hub, now=now)
        circulation.checkout(s, rt.settings, member_key="M0002", item_key="ACC008", actor="t", hub=hub, now=now)
        circulation.renew(s, rt.settings, item_key="ACC001", actor="t", now=now)
        circulation.checkin(s, rt.settings, item_key="ACC002", actor="t", hub=hub, now=now)
        s.commit()
    hub.gate.walk_through(["TAG-ACC004"], people=3)  # secured item: alarm


def test_dashboard_numbers(rt, hub, tagged):
    _activity(rt, hub)
    with rt.factory() as s:
        d = reports.dashboard(s)
    assert d["catalogue"] == {"titles": 12, "items": 12, "items_tagged": 12, "tagged_percent": 100.0}
    assert d["members"] == 4 and d["operators"] == 4
    assert d["circulation"] == {"checkouts": 3, "checkins": 1, "renewals": 1, "active_loans": 2, "overdue_now": 0}
    assert d["gate"] == {"alarms": 1, "passes": 0, "footfall": 3}
    assert d["rfid_clients"] >= 2  # desk reader and gate registered themselves
    assert d["fines_outstanding"] == 0


def test_location_filter_scopes_item_based_metrics(rt, hub, tagged):
    _activity(rt, hub)
    with rt.factory() as s:
        a1 = reports.dashboard(s, location="MAIN-A1")
        b1 = reports.dashboard(s, location="MAIN-B1")
    assert a1["catalogue"]["items"] == 6 and b1["catalogue"]["items"] == 4
    assert a1["circulation"]["checkouts"] == 2 and b1["circulation"]["checkouts"] == 1
    assert a1["circulation"]["active_loans"] == 1 and b1["circulation"]["active_loans"] == 1


def test_date_filter_excludes_old_events(rt, hub, tagged):
    _activity(rt, hub)
    with rt.factory() as s:
        future = reports.dashboard(s, start=utcnow() + timedelta(days=1))
        past = reports.dashboard(s, end=utcnow() - timedelta(days=1))
        window = reports.dashboard(s, start=utcnow() - timedelta(hours=1), end=utcnow() + timedelta(hours=1))
    assert future["circulation"]["checkouts"] == 0 and future["gate"]["alarms"] == 0
    assert past["circulation"]["checkouts"] == 0
    assert window["circulation"]["checkouts"] == 3 and window["gate"]["alarms"] == 1


def test_overdue_and_fines_are_reported(rt, hub, tagged):
    start = utcnow() - timedelta(days=30)
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0003", item_key="ACC005", actor="t", hub=hub, now=start)
        circulation.checkout(s, rt.settings, member_key="M0003", item_key="ACC006", actor="t", hub=hub, now=start)
        circulation.checkin(s, rt.settings, item_key="ACC006", actor="t", hub=hub)  # returned 16 days late
        s.commit()
        d = reports.dashboard(s)
    assert d["circulation"]["overdue_now"] == 1
    assert d["fines_outstanding"] == 16 * rt.settings.fine_per_day


def test_csv_export_matches_loans(rt, hub, tagged):
    _activity(rt, hub)
    with rt.factory() as s:
        text = reports.circulation_csv(s)
        filtered = reports.circulation_csv(s, location="MAIN-B1")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["accession_no"] for r in rows] == ["ACC001", "ACC002", "ACC008"]
    assert rows[0]["renewals"] == "1" and rows[1]["returned_at"] != "" and rows[2]["returned_at"] == ""
    assert [r["accession_no"] for r in csv.DictReader(io.StringIO(filtered))] == ["ACC008"]


def test_reports_over_api(client, rt, hub, tagged, assistant, librarian):
    _activity(rt, hub)
    d = client.get("/reports/dashboard?location=MAIN-A1", headers=assistant).json()
    assert d["filters"]["location"] == "MAIN-A1" and d["catalogue"]["items"] == 6
    csv_resp = client.get("/reports/circulation.csv", headers=librarian)
    assert csv_resp.headers["content-type"].startswith("text/csv") and "ACC001" in csv_resp.text
    assert client.get("/reports/dashboard").status_code == 401


def test_empty_database_reports_zeroes(rt):
    with rt.factory() as s:
        d = reports.dashboard(s)
    assert d["catalogue"]["items"] == 0 and d["catalogue"]["tagged_percent"] == 0.0
    assert d["circulation"]["active_loans"] == 0
