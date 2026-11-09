"""AC 06: a gate alarm records the accession number, captures an image, notifies, and survives outages."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Device, GateEvent, Notification
from app.rfid.events import Event, EventBus
from app.services import circulation, notifications


def events(rt):
    with rt.factory() as s:
        return list(s.scalars(select(GateEvent).order_by(GateEvent.id)))


def test_unsecured_item_alarm_records_image_and_queues_email(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC001"], people=2)  # never checked out: still secured
    alarms = [e for e in events(rt) if e.kind == "alarm"]
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm.accession_no == "ACC001" and alarm.tag_uid == "TAG-ACC001"
    assert alarm.image_path and open(alarm.image_path, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert hub.gate.alarms_sounded == 1
    with rt.factory() as s:
        n = s.scalar(select(Notification))
        assert n.channel == "email" and n.to == rt.settings.security_email
        assert "ACC001" in n.subject and n.attachments == [alarm.image_path]
    foot = [e for e in events(rt) if e.kind == "footfall"]
    assert foot and foot[0].count == 2


def test_checked_out_item_passes_without_alarm(rt, hub, tagged):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0001", item_key="ACC002", actor="t", hub=hub)
        s.commit()
    hub.gate.walk_through(["TAG-ACC002"])
    kinds = [e.kind for e in events(rt)]
    assert "alarm" not in kinds and "pass" in kinds
    assert hub.gate.alarms_sounded == 0


def test_foreign_tags_never_alarm(rt, hub, tagged):
    hub.gate.walk_through(["SKI-PASS-123"])
    assert [e.kind for e in events(rt)] == ["footfall"]


def test_duplicate_reads_in_window_raise_one_alarm(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC003"])
    hub.gate.walk_through(["TAG-ACC003"])  # patron lingers in the field
    assert len([e for e in events(rt) if e.kind == "alarm"]) == 1


def test_same_event_id_is_processed_once(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC004"], event_id="evt-1")
    hub.gate.walk_through(["TAG-ACC004"], event_id="evt-1")
    assert len([e for e in events(rt) if e.kind == "alarm"]) == 1
    assert hub.bus.duplicates == 1


def test_alarm_email_is_delivered_with_retry(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC005"])
    providers = notifications.mock_providers()
    providers["email"].fail_next(1)
    with rt.factory() as s:
        first = notifications.dispatch(s, providers)
        s.commit()
        assert first == {"sent": 0, "failed": 0, "will_retry": 1}
        second = notifications.dispatch(s, providers)
        s.commit()
        assert second["sent"] == 1
    sent = providers["email"].outbox[0]
    assert sent["to"] == rt.settings.security_email and len(sent["attachments"]) == 1


def test_notification_fails_permanently_after_max_attempts(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC005"])
    providers = notifications.mock_providers()
    providers["email"].fail_next(10)
    with rt.factory() as s:
        for _ in range(3):
            result = notifications.dispatch(s, providers, max_attempts=3)
            s.commit()
        assert result["failed"] == 1
        n = s.scalar(select(Notification))
        assert n.status == "failed" and "unavailable" in n.last_error


def test_member_notification_uses_chosen_channel(rt, catalog):
    with rt.factory() as s:
        circulation.checkout(s, rt.settings, member_key="M0002", item_key="ACC001", actor="t")  # M0002 prefers sms
        circulation.checkout(s, rt.settings, member_key="M0003", item_key="ACC002", actor="t")  # M0003 prefers print
        s.commit()
        channels = {n.to: n.channel for n in s.scalars(select(Notification))}
    assert channels == {"9000000002": "sms", "M0003": "print"}


def test_bus_spools_when_handler_down_and_replays_in_order(tmp_path):
    bus = EventBus(max_retries=2, backoff=0)
    handled: list[str] = []
    down = {"value": True}

    def handler(event: Event) -> None:
        if down["value"]:
            raise ConnectionError("application layer offline")
        handled.append(event.payload["n"])

    bus.subscribe("gate.tag_read", handler)
    for n in ("1", "2", "3"):
        bus.publish(Event("gate.tag_read", "gate", {"n": n}))
    assert handled == [] and len(bus.spool) == 3
    down["value"] = False
    assert bus.replay() == 3 and handled == ["1", "2", "3"] and bus.spool == []


def test_bus_replay_keeps_events_that_still_fail():
    bus = EventBus(max_retries=1, backoff=0)

    def always_fail(_: Event) -> None:
        raise RuntimeError("nope")

    bus.subscribe("x", always_fail)
    bus.publish(Event("x", "d", {}))
    assert bus.replay() == 0 and len(bus.spool) == 1


def test_gate_registers_as_rfid_client(rt, hub, tagged):
    hub.gate.walk_through(["TAG-ACC001"])
    with rt.factory() as s:
        assert s.get(Device, "gate-main").kind == "gate"


def test_gate_simulation_endpoint(client, rt, hub, tagged, librarian, assistant):
    assert client.post("/rfid/gate/simulate", json={"uids": ["TAG-ACC001"]}, headers=assistant).status_code == 403
    r = client.post("/rfid/gate/simulate", json={"uids": ["TAG-ACC001"], "people": 1}, headers=librarian)
    assert r.status_code == 200 and r.json()["spooled"] == 0
    assert len([e for e in events(rt) if e.kind == "alarm"]) == 1
