"""AC 02: tag an item, link tag to record, give feedback, handle faults and re-tagging."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.errors import DeviceFault, DomainError
from app.models import AuditLog, RfidTag
from app.rfid import service as rfid


def test_tag_writes_iso28560_style_payload_and_links_record(rt, hub, catalog):
    hub.reader.place("UID-1")
    with rt.factory() as s:
        tag = rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib")
        s.commit()
        assert tag.uid == "UID-1" and tag.secured is True
    mem = hub.reader.memory["UID-1"]
    assert mem.payload["primary_item_id"] == "BC-ACC001"
    assert mem.payload["isil"] == rt.settings.isil
    assert mem.afi == rt.settings.afi_secured == 0xC2
    assert hub.reader.signals[-1] is True  # confirmation beep/flash


def test_tag_recorded_in_audit_log(rt, hub, catalog):
    with rt.factory() as s:
        rfid.tag_item(s, rt.settings, hub, item_key="ACC002", actor="lib", tag_uid="UID-2")
        s.commit()
        row = s.scalar(select(AuditLog).where(AuditLog.action == "tag.write"))
        assert row.actor == "lib" and row.detail["item"] == "ACC002"


def test_cannot_tag_twice_without_replace(rt, hub, catalog):
    with rt.factory() as s:
        rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="UID-1")
        with pytest.raises(DomainError) as e:
            rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="UID-9")
        assert e.value.code == "ALREADY_TAGGED"
    assert hub.reader.signals[-1] is False


def test_tag_uid_cannot_be_reused_on_another_item(rt, hub, catalog):
    with rt.factory() as s:
        rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="UID-1")
        with pytest.raises(DomainError) as e:
            rfid.tag_item(s, rt.settings, hub, item_key="ACC002", actor="lib", tag_uid="UID-1")
        assert e.value.code == "TAG_IN_USE"


def test_replace_retires_old_tag_and_keeps_history(rt, hub, catalog):
    with rt.factory() as s:
        rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="OLD")
        rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="NEW", replace=True)
        s.commit()
        rows = {t.uid: t.status for t in s.scalars(select(RfidTag))}
        assert rows == {"OLD": "retired", "NEW": "active"}


def test_reader_must_see_exactly_one_tag(rt, hub, catalog):
    with rt.factory() as s:
        hub.reader.place()
        with pytest.raises(DomainError) as none:
            rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib")
        assert none.value.code == "NO_TAG"
        hub.reader.place("A", "B")
        with pytest.raises(DomainError) as many:
            rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib")
        assert many.value.code == "MULTIPLE_TAGS"


def test_device_fault_leaves_no_half_written_record(rt, hub, catalog):
    hub.reader.fail_next(1)
    with rt.factory() as s:
        with pytest.raises(DeviceFault):
            rfid.tag_item(s, rt.settings, hub, item_key="ACC001", actor="lib", tag_uid="UID-1")
        s.rollback()
        assert s.scalar(select(RfidTag)) is None
    assert hub.reader.signals[-1] is False


def test_tag_via_api_requires_rfid_permission(client, hub, catalog, librarian, assistant):
    hub.reader.place("UID-API")
    assert client.post("/rfid/tag", json={"item": "ACC003"}, headers=assistant).status_code == 403
    r = client.post("/rfid/tag", json={"item": "ACC003"}, headers=librarian)
    assert r.status_code == 201 and r.json() == {"uid": "UID-API", "secured": True}
    info = client.get("/catalog/items/UID-API", headers=librarian).json()  # lookup by tag uid
    assert info["accession_no"] == "ACC003" and info["tag"]["secured"] is True
