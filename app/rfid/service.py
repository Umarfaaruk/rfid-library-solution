"""RFID workflows: tagging (FR 05), inventory (FR 06) and gate handling (FR 07)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Settings
from app.db import utcnow
from app.errors import DomainError
from app.models import Device, GateEvent, InventorySession, Item, RfidTag
from app.rfid.devices import DeviceHub
from app.rfid.events import Event
from app.services.catalog import resolve_item
from app.services.notifications import enqueue


def touch_device(session: Session, device_id: str, kind: str) -> None:
    dev = session.get(Device, device_id)
    if dev is None:
        session.add(Device(device_id=device_id, kind=kind, last_seen=utcnow()))
    else:
        dev.last_seen = utcnow()


# ---------- tagging ----------


def tag_item(
    session: Session,
    settings: Settings,
    hub: DeviceHub,
    *,
    item_key: str,
    actor: str,
    tag_uid: str | None = None,
    replace: bool = False,
) -> RfidTag:
    """Validate, write the tag, store the link, give audible and visible confirmation."""
    item = resolve_item(session, item_key)
    touch_device(session, hub.reader.device_id, "reader")
    if tag_uid is None:
        found = hub.reader.read_tags()
        if not found:
            hub.reader.signal(False)
            raise DomainError("NO_TAG", "No tag on the reader")
        if len(found) > 1:
            hub.reader.signal(False)
            raise DomainError("MULTIPLE_TAGS", f"{len(found)} tags on the reader, present one at a time")
        tag_uid = found[0]

    existing = session.scalar(select(RfidTag).where(RfidTag.item_id == item.id, RfidTag.status == "active"))
    if existing is not None and not replace:
        hub.reader.signal(False)
        raise DomainError("ALREADY_TAGGED", f"{item.accession_no} already has tag {existing.uid}")
    clash = session.scalar(select(RfidTag).where(RfidTag.uid == tag_uid, RfidTag.status == "active"))
    if clash is not None and clash.item_id != item.id:
        hub.reader.signal(False)
        raise DomainError("TAG_IN_USE", f"Tag {tag_uid} is already linked to another item")

    # Reference items are always secured; others are secured unless currently on loan.
    secured = item.is_reference or item.status == "available"
    payload = {
        "primary_item_id": item.barcode,
        "isil": settings.isil,
        "afi": settings.afi_secured if secured else settings.afi_unsecured,
    }
    try:
        hub.reader.write_tag(tag_uid, payload)
    except Exception:
        hub.reader.signal(False)
        raise
    if existing is not None:
        existing.status, existing.retired_at = "retired", utcnow()
        session.flush()
    if clash is not None:
        tag = clash
        tag.secured = secured
    else:
        tag = RfidTag(uid=tag_uid, item_id=item.id, status="active", secured=secured)
        session.add(tag)
    session.flush()
    hub.reader.signal(True)
    audit(
        session,
        actor,
        "tag.replace" if existing else "tag.write",
        "rfid_tag",
        tag.id,
        {"item": item.accession_no, "uid": tag_uid, "afi": payload["afi"]},
    )
    return tag


# ---------- inventory ----------


def start_inventory(session: Session, hub: DeviceHub, *, location: str, actor: str) -> InventorySession:
    touch_device(session, hub.handheld.device_id, "handheld")
    inv = InventorySession(location=location, device_id=hub.handheld.device_id, actor=actor, scanned=[])
    session.add(inv)
    session.flush()
    audit(session, actor, "inventory.start", "inventory_session", inv.id, {"location": location})
    return inv


def record_scans(session: Session, inv: InventorySession, uids: list[str]) -> int:
    if inv.status != "open":
        raise DomainError("SESSION_CLOSED", "Inventory session is closed")
    seen = set(inv.scanned)
    fresh = [u for u in dict.fromkeys(uids) if u not in seen]  # dedupe, keep order
    inv.scanned = list(inv.scanned) + fresh  # reassign so the JSON column change is tracked
    return len(fresh)


def finish_inventory(session: Session, inv: InventorySession, *, actor: str) -> dict:
    if inv.status != "open":
        raise DomainError("SESSION_CLOSED", "Inventory session is already closed")
    scanned = set(inv.scanned)
    expected_rows = session.execute(
        select(Item.accession_no, RfidTag.uid)
        .join(RfidTag, RfidTag.item_id == Item.id)
        .where(Item.location == inv.location, Item.status == "available", RfidTag.status == "active")
    ).all()
    expected = {uid: acc for acc, uid in expected_rows}
    seen_rows = (
        session.execute(
            select(RfidTag.uid, Item.accession_no, Item.location, Item.status)
            .join(Item, RfidTag.item_id == Item.id)
            .where(RfidTag.uid.in_(scanned), RfidTag.status == "active")
        ).all()
        if scanned
        else []
    )
    known = {uid: (acc, loc, st) for uid, acc, loc, st in seen_rows}
    missing = sorted(acc for uid, acc in expected.items() if uid not in scanned)
    misplaced = sorted(
        (
            {"accession_no": acc, "belongs_in": loc, "found_in": inv.location}
            for _uid, (acc, loc, _st) in known.items()
            if loc != inv.location
        ),
        key=lambda d: d["accession_no"],
    )
    on_loan_found = sorted(acc for uid, (acc, loc, st) in known.items() if st == "on_loan")
    unknown = sorted(u for u in scanned if u not in known)
    result = {
        "location": inv.location,
        "expected": len(expected),
        "scanned": len(scanned),
        "found_in_place": sum(1 for uid in scanned if uid in expected),
        "missing": missing,
        "misplaced": misplaced,
        "on_loan_but_on_shelf": on_loan_found,
        "unknown_tags": unknown,
    }
    inv.result, inv.status, inv.finished_at = result, "closed", utcnow()
    audit(
        session,
        actor,
        "inventory.finish",
        "inventory_session",
        inv.id,
        {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()},
    )
    return result


# ---------- gate ----------


def handle_gate_tag(
    session: Session,
    settings: Settings,
    hub: DeviceHub,
    event: Event,
    *,
    now: datetime | None = None,
) -> GateEvent | None:
    """Alarm if the tag is still secured. Duplicate reads inside the window are suppressed."""
    now = now or event.at
    uid = event.payload["uid"]
    location = event.payload.get("location")
    touch_device(session, event.device_id, "gate")
    window_start = now - timedelta(seconds=settings.gate_dedupe_seconds)
    recent = session.scalar(
        select(GateEvent).where(
            GateEvent.tag_uid == uid, GateEvent.at >= window_start, GateEvent.kind.in_(["alarm", "pass"])
        )
    )
    if recent is not None:
        return None
    tag = session.scalar(select(RfidTag).where(RfidTag.uid == uid, RfidTag.status == "active"))
    if tag is None:
        return None  # not one of our tags, for example a ski pass: ignore, never alarm
    item = session.get(Item, tag.item_id)
    if not tag.secured:
        ev = GateEvent(
            at=now,
            kind="pass",
            device_id=event.device_id,
            location=location,
            tag_uid=uid,
            accession_no=item.accession_no,
        )
        session.add(ev)
        return ev
    image = settings.gate_images_dir / f"alarm_{now:%Y%m%dT%H%M%S}_{item.accession_no}.png"
    hub.camera.capture(image, f"ALARM {item.accession_no} {item.bib.title[:40]}")
    hub.gate.sound_alarm()
    ev = GateEvent(
        at=now,
        kind="alarm",
        device_id=event.device_id,
        location=location,
        tag_uid=uid,
        accession_no=item.accession_no,
        image_path=str(image),
    )
    session.add(ev)
    enqueue(
        session,
        "email",
        settings.security_email,
        f"Gate alarm: {item.accession_no}",
        f"Item {item.accession_no} ({item.bib.title}) triggered the gate at {now:%Y-%m-%d %H:%M:%S} UTC.",
        [str(image)],
    )
    audit(session, "gate", "gate.alarm", "item", item.accession_no, {"uid": uid, "device": event.device_id}, now=now)
    return ev


def handle_gate_footfall(session: Session, event: Event) -> GateEvent:
    touch_device(session, event.device_id, "gate")
    ev = GateEvent(
        at=event.at,
        kind="footfall",
        device_id=event.device_id,
        location=event.payload.get("location"),
        count=int(event.payload.get("count", 1)),
    )
    session.add(ev)
    return ev


def wire_gate(hub: DeviceHub, settings: Settings, session_factory) -> None:
    """Subscribe gate handlers. Each event is handled in its own short transaction."""
    from app.db import session_scope

    def on_tag(event: Event) -> None:
        with session_scope(session_factory) as s:
            handle_gate_tag(s, settings, hub, event)

    def on_foot(event: Event) -> None:
        with session_scope(session_factory) as s:
            handle_gate_footfall(s, event)

    hub.bus.subscribe("gate.tag_read", on_tag)
    hub.bus.subscribe("gate.footfall", on_foot)
