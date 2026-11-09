"""NCIP 2.0 (ANSI/NISO Z39.83) adapter for the circulation core messages.

Implemented: CheckOutItem, CheckInItem, RenewItem, LookupItem, LookupUser. These come from the
NCIP Implementers Group core message set. The adapter is a thin translation layer onto the same
circulation service the UI uses, so blocks, fines and audit rules cannot differ by protocol.
Subset only: no claim of formal conformance. XML is parsed with defusedxml (no DTD, no entities).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405  (used only to BUILD responses; parsing uses defusedxml)

from defusedxml import ElementTree as SafeET
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import DomainError
from app.rfid.devices import DeviceHub
from app.services import circulation
from app.services.catalog import resolve_item, resolve_member

NS = "http://www.niso.org/2008/ncip"
SCHEMA = "http://www.niso.org/schemas/ncip/v2_02/ncip_v2_02.xsd"

# Our internal reason codes -> NCIP problem types (wording follows commonly used NCIP problem schemes).
PROBLEM_TYPES = {
    "MEMBER_NOT_FOUND": "Unknown User",
    "ITEM_NOT_FOUND": "Unknown Item",
    "MEMBER_BLOCKED": "User Blocked",
    "FINE_LIMIT_EXCEEDED": "User Blocked",
    "REFERENCE_ITEM": "Item Does Not Circulate",
    "ITEM_UNAVAILABLE": "Item Not Available",
    "LOAN_LIMIT_REACHED": "User Ineligible to Check Out This Item",
    "NOT_ON_LOAN": "Item Not Checked Out",
    "RENEWAL_LIMIT_REACHED": "Item Not Renewable",
    "OVERDUE_RENEWAL": "Item Not Renewable",
    "DEVICE_FAULT": "Temporary Processing Failure",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root, name: str) -> str | None:
    for el in root.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def _q(name: str) -> str:
    return f"{{{NS}}}{name}"


def _envelope(kind: str) -> tuple[ET.Element, ET.Element]:
    ET.register_namespace("", NS)
    root = ET.Element(_q("NCIPMessage"), {"version": SCHEMA})
    resp = ET.SubElement(root, _q(kind))
    return root, resp


def _ids(resp: ET.Element, user: str | None, item: str | None) -> None:
    if user:
        ET.SubElement(ET.SubElement(resp, _q("UserId")), _q("UserIdentifierValue")).text = user
    if item:
        ET.SubElement(ET.SubElement(resp, _q("ItemId")), _q("ItemIdentifierValue")).text = item


def _problem(kind: str, err: DomainError) -> bytes:
    root, resp = _envelope(kind)
    prob = ET.SubElement(resp, _q("Problem"))
    ET.SubElement(prob, _q("ProblemType")).text = PROBLEM_TYPES.get(err.code, "Unknown Problem")
    ET.SubElement(prob, _q("ProblemDetail")).text = err.message
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def handle(xml_bytes: bytes, session: Session, settings: Settings, hub: DeviceHub | None, *, actor: str) -> bytes:
    try:
        root = SafeET.fromstring(xml_bytes, forbid_dtd=True)
    except Exception as exc:
        raise DomainError("NCIP_BAD_XML", f"Unreadable NCIP message: {type(exc).__name__}") from exc
    if _local(root.tag) != "NCIPMessage" or len(root) == 0:
        raise DomainError("NCIP_BAD_XML", "Expected an NCIPMessage with one service element")
    service = _local(root[0].tag)
    user_id = _find_text(root[0], "UserIdentifierValue")
    item_id = _find_text(root[0], "ItemIdentifierValue")
    reply = f"{service}Response"
    try:
        if service == "CheckOutItem":
            loan = circulation.checkout(
                session, settings, member_key=user_id or "", item_key=item_id or "", actor=actor, source="ncip", hub=hub
            )
            out_root, resp = _envelope(reply)
            _ids(resp, user_id, item_id)
            ET.SubElement(resp, _q("DateDue")).text = loan.due_at.strftime("%Y-%m-%dT%H:%M:%S")
        elif service == "CheckInItem":
            loan = circulation.checkin(session, settings, item_key=item_id or "", actor=actor, source="ncip", hub=hub)
            out_root, resp = _envelope(reply)
            _ids(resp, loan.member.member_no, item_id)
        elif service == "RenewItem":
            loan = circulation.renew(session, settings, item_key=item_id or "", actor=actor, source="ncip")
            out_root, resp = _envelope(reply)
            _ids(resp, user_id, item_id)
            ET.SubElement(resp, _q("DateForReturn")).text = loan.due_at.strftime("%Y-%m-%dT%H:%M:%S")
        elif service == "LookupItem":
            item = resolve_item(session, item_id or "")
            out_root, resp = _envelope(reply)
            _ids(resp, None, item.accession_no)
            status = "On Loan" if item.status == "on_loan" else "Available On Shelf"
            ET.SubElement(resp, _q("CirculationStatus")).text = status
            ET.SubElement(resp, _q("Title")).text = item.bib.title
        elif service == "LookupUser":
            member = resolve_member(session, user_id or "")
            out_root, resp = _envelope(reply)
            _ids(resp, member.member_no, None)
            ET.SubElement(resp, _q("UnstructuredPersonalUserName")).text = member.name
            ET.SubElement(resp, _q("AccountBalance")).text = f"{member.fines_due:.2f}"
            if member.blocked or member.fines_due >= settings.fine_limit:
                ET.SubElement(resp, _q("BlockOrTrap")).text = "Block Check Out"
        else:
            raise DomainError("NCIP_UNSUPPORTED", f"Service {service} is not implemented")
    except DomainError as err:
        if err.code in {"NCIP_UNSUPPORTED", "NCIP_BAD_XML"}:
            raise
        return _problem(reply, err)
    return ET.tostring(out_root, encoding="utf-8", xml_declaration=True)
