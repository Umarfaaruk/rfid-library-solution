"""AC 05: handheld shelf inventory reports missing, misplaced and unknown tags."""

from __future__ import annotations

import pytest

from app.errors import DomainError
from app.rfid import service as rfid
from app.services import circulation


def test_inventory_finds_missing_misplaced_unknown_and_loaned(rt, hub, tagged):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC006", actor="t", hub=hub)
        s.commit()

    # MAIN-A1 expects ACC001..ACC005 on the shelf (ACC006 is on loan).
    hub.handheld.load(
        [
            "TAG-ACC001",
            "TAG-ACC002",
            "TAG-ACC003",  # in place
            "TAG-ACC008",  # belongs to MAIN-B1: misplaced
            "TAG-ACC006",  # on loan but found on shelf
            "TAG-GHOST",  # not one of ours
            "TAG-ACC001",  # duplicate read
        ]
    )
    with rt.factory() as s:
        inv = rfid.start_inventory(s, hub, location="MAIN-A1", actor="clerk")
        added = rfid.record_scans(s, inv, hub.handheld.read_batch())
        assert added == 6  # duplicate collapsed
        result = rfid.finish_inventory(s, inv, actor="clerk")
        s.commit()

    assert result["expected"] == 5
    assert result["found_in_place"] == 3
    assert result["missing"] == ["ACC004", "ACC005"]
    assert result["misplaced"] == [{"accession_no": "ACC008", "belongs_in": "MAIN-B1", "found_in": "MAIN-A1"}]
    assert result["on_loan_but_on_shelf"] == ["ACC006"]
    assert result["unknown_tags"] == ["TAG-GHOST"]


def test_closed_session_rejects_more_scans(rt, hub, tagged):
    with rt.factory() as s:
        inv = rfid.start_inventory(s, hub, location="MAIN-A1", actor="clerk")
        rfid.finish_inventory(s, inv, actor="clerk")
        with pytest.raises(DomainError) as e:
            rfid.record_scans(s, inv, ["TAG-ACC001"])
        assert e.value.code == "SESSION_CLOSED"


def test_inventory_over_api_with_permissions(client, hub, tagged, librarian, assistant):
    assert client.post("/rfid/inventory", json={"location": "MAIN-B1"}, headers=assistant).status_code == 403
    sid = client.post("/rfid/inventory", json={"location": "MAIN-B1"}, headers=librarian).json()["session_id"]
    hub.handheld.load(["TAG-ACC007", "TAG-ACC008"])
    scan = client.post(f"/rfid/inventory/{sid}/scan", json={"from_device": True}, headers=librarian).json()
    assert scan == {"added": 2, "total": 2}
    done = client.post(f"/rfid/inventory/{sid}/finish", headers=librarian).json()
    assert done["missing"] == ["ACC009", "ACC010"] and done["misplaced"] == []
    assert client.post("/rfid/inventory/9999/finish", headers=librarian).status_code == 404
