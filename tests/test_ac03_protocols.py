"""AC 03: loans through NCIP 2.0 and SIP2 produce the same results and history as the UI."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.adapters import sip2
from app.adapters.sip2 import Sip2Handler, parse_fields
from app.models import LoanEvent

NCIP_NS = "http://www.niso.org/2008/ncip"


def ncip_msg(service: str, user: str | None = None, item: str | None = None) -> bytes:
    u = f"<UserId><UserIdentifierValue>{user}</UserIdentifierValue></UserId>" if user else ""
    i = f"<ItemId><ItemIdentifierValue>{item}</ItemIdentifierValue></ItemId>" if item else ""
    return (
        f'<?xml version="1.0"?><NCIPMessage xmlns="{NCIP_NS}"><{service}>'
        f"<InitiationHeader><FromAgencyId><AgencyId>SELF</AgencyId></FromAgencyId></InitiationHeader>"
        f"{u}{i}</{service}></NCIPMessage>"
    ).encode()


def post(client, headers, body: bytes):
    return client.post("/integrations/ncip", content=body, headers={**headers, "Content-Type": "application/xml"})


# ---------------- NCIP ----------------


def test_ncip_checkout_checkin_renew_roundtrip(client, rt, catalog, kiosk):
    r = post(client, kiosk, ncip_msg("CheckOutItem", "M0001", "ACC001"))
    assert (
        r.status_code == 200
        and b"CheckOutItemResponse" in r.content
        and b"<ns0:DateDue>" in r.content
        or b"DateDue" in r.content
    )
    assert b"Problem" not in r.content
    assert b"RenewItemResponse" in post(client, kiosk, ncip_msg("RenewItem", "M0001", "ACC001")).content
    back = post(client, kiosk, ncip_msg("CheckInItem", item="ACC001"))
    assert b"CheckInItemResponse" in back.content and b"Problem" not in back.content
    with rt.factory() as s:
        events = s.execute(select(LoanEvent.kind, LoanEvent.source).order_by(LoanEvent.id)).all()
    assert events == [("checkout", "ncip"), ("renew", "ncip"), ("checkin", "ncip")]


def test_ncip_problem_types_for_rule_violations(client, rt, catalog, kiosk):
    assert b"Item Does Not Circulate" in post(client, kiosk, ncip_msg("CheckOutItem", "M0001", "ACC011")).content
    assert b"Unknown Item" in post(client, kiosk, ncip_msg("CheckOutItem", "M0001", "NOPE")).content
    assert b"Unknown User" in post(client, kiosk, ncip_msg("CheckOutItem", "GHOST", "ACC001")).content
    with rt.factory() as s:
        from app.models import Member

        s.query(Member).filter_by(member_no="M0002").one().blocked = True
        s.commit()
    assert b"User Blocked" in post(client, kiosk, ncip_msg("CheckOutItem", "M0002", "ACC001")).content


def test_ncip_lookup_user_and_item(client, catalog, kiosk):
    post(client, kiosk, ncip_msg("CheckOutItem", "M0001", "ACC001"))
    item = post(client, kiosk, ncip_msg("LookupItem", item="ACC001")).content
    assert b"On Loan" in item and b"Title of ACC001" in item
    user = post(client, kiosk, ncip_msg("LookupUser", user="M0001")).content
    assert b"Asha Rao" in user and b"AccountBalance" in user


def test_ncip_rejects_dtd_and_entities(client, kiosk):
    evil = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<NCIPMessage xmlns="http://www.niso.org/2008/ncip"><LookupUser>&xxe;</LookupUser></NCIPMessage>'
    )
    r = post(client, kiosk, evil)
    assert r.status_code == 400 and r.json()["code"] == "NCIP_BAD_XML"
    assert b"root:" not in r.content


def test_ncip_requires_integration_role(client, catalog, librarian, assistant):
    assert post(client, librarian, ncip_msg("LookupUser", user="M0001")).status_code == 403
    assert post(client, assistant, ncip_msg("LookupUser", user="M0001")).status_code == 403
    assert client.post("/integrations/ncip", content=ncip_msg("LookupUser", user="M0001")).status_code == 401


def test_ncip_unsupported_service(client, kiosk):
    r = post(client, kiosk, ncip_msg("CancelRequestItem"))
    assert r.status_code == 400 and r.json()["code"] == "NCIP_UNSUPPORTED"


# ---------------- SIP2 ----------------


def handler(rt, hub) -> Sip2Handler:
    h = Sip2Handler(rt.factory, rt.settings, hub)
    assert h.handle("9300CNselfcheck|COKiosk#2026|CPDESK|") == "941"
    return h


def co(member, item):
    return f"11NN{sip2.ts()}{' ' * 18}AOMAIN|AA{member}|AB{item}|AC|"


def test_sip2_login_required_and_validated(rt, hub, catalog):
    h = Sip2Handler(rt.factory, rt.settings, hub)
    assert h.handle(co("M0001", "ACC001")) == "940"  # not logged in
    assert h.handle("9300CNselfcheck|COwrong|CPDESK|") == "940"
    assert h.handle("9300CNlibrarian|COLibra#2026|CPDESK|") == "940"  # staff role cannot act as a kiosk
    assert h.handle("9300CNselfcheck|COKiosk#2026|CPDESK|") == "941"


def test_sip2_status_message_lists_supported_messages(rt, hub):
    h = handler(rt, hub)
    reply = h.handle("99" + "0" + "060" + "2.00")
    assert reply.startswith("98Y") and "BXYYYNYNYNNNYNNNYN|" in reply and reply.endswith("|")
    fixed = 2 + 6 + 3 + 3 + 18 + 4  # code, six flags, timeout, retries, date, protocol version
    assert parse_fields(reply[fixed:])["AO"] == "MAIN"


def test_sip2_checkout_checkin_renew_item_info(rt, hub, catalog):
    h = handler(rt, hub)
    out = h.handle(co("M0001", "ACC001"))
    assert out.startswith("121NUN") and len(out[6:24]) == 18
    f = parse_fields(out[24:])
    assert f["AB"] == "ACC001" and f["AJ"] == "Title of ACC001" and f["AH"][:8].isdigit()

    info = h.handle(f"17{sip2.ts()}AOMAIN|ABACC001|")
    assert info.startswith("1804")  # circulation status 04 = charged out
    renew = h.handle(f"29NN{sip2.ts()}{' ' * 18}AOMAIN|AAM0001|ABACC001|")
    assert renew.startswith("301YNN")
    back = h.handle(f"09N{sip2.ts()}{sip2.ts()}APMAIN|AOMAIN|ABACC001|")
    assert back.startswith("101YNN") and parse_fields(back[24:])["AQ"] == "MAIN-A1"
    assert h.handle(f"17{sip2.ts()}AOMAIN|ABACC001|").startswith("1803")  # available again
    with rt.factory() as s:
        kinds = s.execute(select(LoanEvent.kind, LoanEvent.source).order_by(LoanEvent.id)).all()
    assert kinds == [("checkout", "sip2"), ("renew", "sip2"), ("checkin", "sip2")]


def test_sip2_refusals_carry_screen_message(rt, hub, catalog):
    h = handler(rt, hub)
    refused = h.handle(co("M0001", "ACC011"))  # reference item
    assert refused.startswith("120NUN")
    assert "reference item" in parse_fields(refused[24:])["AF"]
    assert h.handle(co("M0001", "NOPE")).startswith("120NUN")


def test_sip2_patron_status_flags_blocks(rt, hub, catalog):
    h = handler(rt, hub)
    ok = h.handle(f"23000{sip2.ts()}AOMAIN|AAM0001|")
    assert ok.startswith("24" + " " * 14) and parse_fields(ok[41:])["BL"] == "Y"
    with rt.factory() as s:
        from app.models import Member

        s.query(Member).filter_by(member_no="M0001").one().blocked = True
        s.commit()
    blocked = h.handle(f"23000{sip2.ts()}AOMAIN|AAM0001|")
    assert blocked[2] == "Y"  # charge privileges denied
    unknown = h.handle(f"23000{sip2.ts()}AOMAIN|AAGHOST|")
    assert parse_fields(unknown[41:])["BL"] == "N"


def test_sip2_unknown_message_asks_for_resend(rt, hub):
    assert handler(rt, hub).handle("ZZ123") == "96"


def test_sip2_over_tcp(rt, hub, catalog):
    async def scenario():
        server = await sip2.serve(lambda: Sip2Handler(rt.factory, rt.settings, hub), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def ask(msg: str) -> str:
            writer.write((msg + "\r").encode())
            await writer.drain()
            return (await reader.readuntil(b"\r")).decode().strip("\r")

        assert await ask("9300CNselfcheck|COKiosk#2026|CPDESK|") == "941"
        reply = await ask(co("M0003", "ACC004"))
        writer.close()
        server.close()
        await server.wait_closed()
        return reply

    reply = asyncio.run(scenario())
    assert reply.startswith("121NUN") and "ACC004" in reply


def test_same_rules_apply_over_both_protocols(client, rt, hub, catalog, kiosk):
    """The reference-item refusal is identical whichever door the request comes through."""
    ncip = post(client, kiosk, ncip_msg("CheckOutItem", "M0001", "ACC012")).content
    sip = handler(rt, hub).handle(co("M0001", "ACC012"))
    assert b"Item Does Not Circulate" in ncip and "reference item" in sip
