"""SIP2 (3M Standard Interchange Protocol v2) boundary: message parsing, handler and asyncio TCP server.

Implemented pairs: 93/94 login, 99/98 SC-ACS status, 23/24 patron status, 17/18 item information,
11/12 checkout, 09/10 checkin, 29/30 renew. Message layouts follow the public SIP2 developer guide
and vendor message tables. Subset only: no checksum/sequence (AY/AZ) handling and no formal
certification claim. The handler is one-per-connection and keeps the login state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import session_scope, utcnow
from app.errors import DomainError
from app.rfid.devices import DeviceHub
from app.security import authenticate_password
from app.services import circulation
from app.services.catalog import resolve_item, resolve_member

CR = "\r"
BX = "YYYNYNYNNNYNNNYN"  # supported-messages map for the 98 response, in SIP2 position order


def ts(now: datetime | None = None) -> str:
    """SIP2 transaction date: YYYYMMDDZZZZHHMMSS (ZZZZ = 4 spaces for local/UTC)."""
    return (now or utcnow()).strftime("%Y%m%d    %H%M%S")


def parse_fields(tail: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in tail.split("|"):
        if len(part) >= 2:
            out[part[:2]] = part[2:]
    return out


def _fields(**pairs: str | None) -> str:
    return "".join(f"{k}{v}|" for k, v in pairs.items() if v is not None)


class Sip2Handler:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        hub: DeviceHub | None = None,
        *,
        institution: str = "MAIN",
        require_login: bool = True,
    ):
        self.factory, self.settings, self.hub = factory, settings, hub
        self.institution = institution
        self.require_login = require_login
        self.user: str | None = None

    def handle(self, line: str) -> str:
        line = line.strip("\r\n")
        if len(line) < 2:
            return "96"
        code = line[:2]
        if self.require_login and self.user is None and code not in {"93", "99"}:
            return "940"
        method: Callable[[str], str] | None = getattr(self, f"_m{code}", None)
        if method is None:
            return "96"  # request SC resend: message not understood
        try:
            return method(line[2:])
        except DomainError as err:
            return self._failure(code, err)

    # ----- session -----
    def _m93(self, body: str) -> str:
        f = parse_fields(body[2:])  # UID algorithm (1) + PWD algorithm (1), then fields CN CO CP
        with session_scope(self.factory) as s:
            user = authenticate_password(s, f.get("CN", ""), f.get("CO", ""))
            ok = user is not None and user.role in {"integration", "admin"}
        self.user = f.get("CN") if ok else None
        return "94" + ("1" if ok else "0")

    def _m99(self, body: str) -> str:
        return (f"98YYYYNN030003{ts()}2.00" + _fields(AO=self.institution, AM="RFID Library Prototype", BX=BX)).rstrip(
            "|"
        ) + "|"

    # ----- patron / item -----
    def _m23(self, body: str) -> str:
        f = parse_fields(body[3 + 18 :])
        with session_scope(self.factory) as s:
            try:
                m = resolve_member(s, f.get("AA", ""))
            except DomainError:
                return "24" + " " * 14 + "000" + ts() + _fields(AO=self.institution, AA=f.get("AA", ""), BL="N")
            status = [" "] * 14
            if m.blocked:
                status[0] = "Y"
            if m.fines_due >= self.settings.fine_limit:
                status[0] = status[10] = "Y"
            return (
                "24" + "".join(status) + "000" + ts() + _fields(AO=self.institution, AA=m.member_no, AE=m.name, BL="Y")
            )

    def _m17(self, body: str) -> str:
        f = parse_fields(body[18:])
        with session_scope(self.factory) as s:
            try:
                item = resolve_item(s, f.get("AB", ""))
            except DomainError:
                return "1801" + "01" + "00" + ts() + _fields(AB=f.get("AB", ""), AJ="")
            status = "04" if item.status == "on_loan" else "03"
            return "18" + status + "00" + "01" + ts() + _fields(AB=item.accession_no, AJ=item.bib.title)

    # ----- circulation -----
    def _m11(self, body: str) -> str:
        f = parse_fields(body[1 + 1 + 18 + 18 :])
        with session_scope(self.factory) as s:
            loan = circulation.checkout(
                s,
                self.settings,
                member_key=f.get("AA", ""),
                item_key=f.get("AB", ""),
                actor=self.user or "sip2",
                source="sip2",
                hub=self.hub,
            )
            return (
                "121NUN"
                + ts()
                + _fields(
                    AO=self.institution,
                    AA=f["AA"],
                    AB=f["AB"],
                    AJ=loan.item.bib.title,
                    AH=loan.due_at.strftime("%Y%m%d    %H%M%S"),
                )
            )

    def _m09(self, body: str) -> str:
        f = parse_fields(body[1 + 18 + 18 :])
        with session_scope(self.factory) as s:
            loan = circulation.checkin(
                s, self.settings, item_key=f.get("AB", ""), actor=self.user or "sip2", source="sip2", hub=self.hub
            )
            return (
                "101YNN"
                + ts()
                + _fields(AO=self.institution, AB=f["AB"], AQ=loan.item.location, AJ=loan.item.bib.title)
            )

    def _m29(self, body: str) -> str:
        f = parse_fields(body[1 + 1 + 18 + 18 :])
        with session_scope(self.factory) as s:
            loan = circulation.renew(
                s, self.settings, item_key=f.get("AB", ""), actor=self.user or "sip2", source="sip2"
            )
            return (
                "301YNN"
                + ts()
                + _fields(
                    AO=self.institution,
                    AA=f.get("AA", ""),
                    AB=f["AB"],
                    AJ=loan.item.bib.title,
                    AH=loan.due_at.strftime("%Y%m%d    %H%M%S"),
                )
            )

    def _failure(self, code: str, err: DomainError) -> str:
        screen = _fields(AF=err.message)
        if code == "11":
            return "120NUN" + ts() + _fields(AO=self.institution) + screen
        if code == "09":
            return "100NUN" + ts() + _fields(AO=self.institution) + screen
        if code == "29":
            return "300NUN" + ts() + _fields(AO=self.institution) + screen
        return "96"


async def serve(handler_factory: Callable[[], Sip2Handler], host: str = "127.0.0.1", port: int = 6001):
    """Start a SIP2 TCP listener. Messages end with CR. Blocking DB work runs in a thread."""

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = handler_factory()
        try:
            while data := await reader.readuntil(CR.encode()):
                reply = await asyncio.to_thread(handler.handle, data.decode("utf-8", "replace"))
                writer.write((reply + CR).encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(client, host, port)
