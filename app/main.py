"""FastAPI application factory. Every route checks a permission server-side (fail closed)."""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import __version__
from app.adapters import ncip
from app.audit import audit
from app.bootstrap import Runtime
from app.errors import DomainError, NotFound
from app.models import AuditLog, Member, RfidTag, User
from app.ops import backup as backup_ops
from app.ops.schema import LATEST, current_version
from app.rfid import service as rfid
from app.rfid.devices import DeviceHub, mock_hub
from app.security import (
    Principal,
    authenticate_card,
    authenticate_password,
    hash_password,
    issue_token,
    read_token,
)
from app.services import circulation, migration, reports
from app.services import notifications as notif
from app.services.catalog import resolve_item, resolve_member, search_catalog

log = logging.getLogger("rfidlib")


class LoginIn(BaseModel):
    username: str
    password: str


class CardIn(BaseModel):
    card_uid: str


class CheckoutIn(BaseModel):
    member: str
    item: str


class ItemIn(BaseModel):
    item: str


class MemberIn(BaseModel):
    member_no: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    email: str | None = None
    phone: str | None = None
    card_uid: str | None = None
    notify_channel: str = Field(default="email", pattern="^(email|sms|print)$")


class PayIn(BaseModel):
    amount: float


class TagIn(BaseModel):
    item: str
    tag_uid: str | None = None
    replace: bool = False


class InventoryStartIn(BaseModel):
    location: str


class ScanIn(BaseModel):
    uids: list[str] = []
    from_device: bool = False


class GateWalkIn(BaseModel):
    uids: list[str] = []
    people: int = 1


class UserIn(BaseModel):
    username: str
    role: str = Field(pattern="^(admin|librarian|assistant|integration)$")
    password: str = Field(min_length=8)
    card_uid: str | None = None


def create_app(rt: Runtime, hub: DeviceHub | None = None, providers: dict | None = None) -> FastAPI:
    app = FastAPI(title="RFID Library Solution", version=__version__)
    app.state.rt = rt
    app.state.hub = hub or mock_hub(afi_secured=rt.settings.afi_secured, afi_unsecured=rt.settings.afi_unsecured)
    app.state.providers = providers or notif.mock_providers()
    rfid.wire_gate(app.state.hub, rt.settings, rt.factory)

    # ----- plumbing -----
    @app.exception_handler(DomainError)
    async def domain_error(_: Request, err: DomainError):
        return JSONResponse(status_code=err.status, content=err.as_dict())

    def get_session() -> Iterator[Session]:
        session = rt.factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise DomainError("UNAUTHENTICATED", "Missing bearer token", status=401)
        return read_token(authorization[7:], rt.settings)

    def need(permission: str):
        def dep(user: Principal = Depends(principal)) -> Principal:
            user.require(permission)
            return user

        return dep

    # ----- public -----
    static_dir = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/ui/{name}", include_in_schema=False)
    def ui_asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise NotFound("ASSET_NOT_FOUND", "No such asset")
        return FileResponse(static_dir / name, headers={"Cache-Control": "no-cache"})

    @app.get("/opac/search")
    def opac_search(q: str, limit: int = 20, session: Session = Depends(get_session)):
        """Public catalogue search. Read-only and returns no member or circulation data."""
        hits = search_catalog(session, q[:100], limit=min(max(limit, 1), 50))
        return {"query": q, "results": hits}

    @app.get("/health")
    def health(session: Session = Depends(get_session)):
        session.execute(text("SELECT 1"))
        version = current_version(rt.engine)
        return {
            "status": "ok" if version == LATEST else "schema_outdated",
            "app_version": __version__,
            "schema_version": version,
            "schema_expected": LATEST,
        }

    @app.post("/auth/login")
    def login(body: LoginIn, session: Session = Depends(get_session)):
        user = authenticate_password(session, body.username, body.password)
        if user is None:
            audit(session, body.username, "auth.fail", "user", body.username, {"method": "password"})
            session.commit()
            raise DomainError("BAD_CREDENTIALS", "Invalid username or password", status=401)
        audit(session, user.username, "auth.login", "user", user.username, {"method": "password"})
        return {"token": issue_token(user, rt.settings), "role": user.role}

    @app.post("/auth/card-login")
    def card_login(body: CardIn, session: Session = Depends(get_session)):
        user = authenticate_card(session, body.card_uid)
        if user is None:
            audit(session, "unknown", "auth.fail", "user", None, {"method": "card", "card_uid": body.card_uid})
            session.commit()
            raise DomainError("BAD_CREDENTIALS", "Card not recognised", status=401)
        audit(session, user.username, "auth.login", "user", user.username, {"method": "card"})
        return {"token": issue_token(user, rt.settings), "role": user.role}

    # ----- catalogue -----
    @app.get("/catalog/search")
    def catalog_search(
        q: str,
        limit: int = 20,
        offset: int = 0,
        _: Principal = Depends(need("catalog.read")),
        session: Session = Depends(get_session),
    ):
        return {"query": q, "results": search_catalog(session, q, limit=min(limit, 100), offset=offset)}

    @app.get("/catalog/items/{key}")
    def catalog_item(key: str, _: Principal = Depends(need("catalog.read")), session: Session = Depends(get_session)):
        item = resolve_item(session, key)
        tag = session.scalar(select(RfidTag).where(RfidTag.item_id == item.id, RfidTag.status == "active"))
        return {
            "accession_no": item.accession_no,
            "barcode": item.barcode,
            "title": item.bib.title,
            "author": item.bib.author,
            "location": item.location,
            "status": item.status,
            "reference": item.is_reference,
            "tag": None if tag is None else {"uid": tag.uid, "secured": tag.secured},
        }

    # ----- members -----
    @app.post("/members", status_code=201)
    def create_member(
        body: MemberIn, user: Principal = Depends(need("members.write")), session: Session = Depends(get_session)
    ):
        if session.scalar(select(Member).where(Member.member_no == body.member_no)):
            raise DomainError("MEMBER_EXISTS", f"Member {body.member_no} already exists", status=409)
        m = Member(**body.model_dump())
        session.add(m)
        audit(session, user.username, "member.create", "member", body.member_no, {"email": body.email})
        return {"member_no": m.member_no}

    @app.get("/members/{key}")
    def get_member(key: str, _: Principal = Depends(need("members.read")), session: Session = Depends(get_session)):
        m = resolve_member(session, key)
        return {
            "member_no": m.member_no,
            "name": m.name,
            "blocked": m.blocked,
            "fines_due": m.fines_due,
            "notify_channel": m.notify_channel,
            "loans": circulation.member_loans(session, m.member_no),
        }

    @app.post("/members/{key}/fines/pay")
    def pay(
        key: str, body: PayIn, user: Principal = Depends(need("members.write")), session: Session = Depends(get_session)
    ):
        m = circulation.pay_fine(session, member_key=key, amount=body.amount, actor=user.username)
        return {"member_no": m.member_no, "fines_due": m.fines_due}

    @app.post("/members/{key}/block")
    def block(
        key: str,
        blocked: bool = True,
        user: Principal = Depends(need("members.write")),
        session: Session = Depends(get_session),
    ):
        m = resolve_member(session, key)
        m.blocked = blocked
        audit(session, user.username, "member.block" if blocked else "member.unblock", "member", m.member_no)
        return {"member_no": m.member_no, "blocked": m.blocked}

    # ----- circulation -----
    @app.post("/circulation/checkout")
    def api_checkout(
        body: CheckoutIn, user: Principal = Depends(need("circulation.write")), session: Session = Depends(get_session)
    ):
        loan = circulation.checkout(
            session, rt.settings, member_key=body.member, item_key=body.item, actor=user.username, hub=app.state.hub
        )
        return {"loan_id": loan.id, "due_at": loan.due_at.isoformat()}

    @app.post("/circulation/checkin")
    def api_checkin(
        body: ItemIn, user: Principal = Depends(need("circulation.write")), session: Session = Depends(get_session)
    ):
        loan = circulation.checkin(session, rt.settings, item_key=body.item, actor=user.username, hub=app.state.hub)
        return {"loan_id": loan.id, "fine": loan.fine_charged}

    @app.post("/circulation/renew")
    def api_renew(
        body: ItemIn, user: Principal = Depends(need("circulation.write")), session: Session = Depends(get_session)
    ):
        loan = circulation.renew(session, rt.settings, item_key=body.item, actor=user.username)
        return {"loan_id": loan.id, "due_at": loan.due_at.isoformat(), "renewals": loan.renewals}

    # ----- RFID -----
    @app.post("/rfid/tag", status_code=201)
    def api_tag(body: TagIn, user: Principal = Depends(need("rfid.use")), session: Session = Depends(get_session)):
        tag = rfid.tag_item(
            session,
            rt.settings,
            app.state.hub,
            item_key=body.item,
            actor=user.username,
            tag_uid=body.tag_uid,
            replace=body.replace,
        )
        return {"uid": tag.uid, "secured": tag.secured}

    @app.post("/rfid/inventory", status_code=201)
    def inv_start(
        body: InventoryStartIn, user: Principal = Depends(need("rfid.use")), session: Session = Depends(get_session)
    ):
        inv = rfid.start_inventory(session, app.state.hub, location=body.location, actor=user.username)
        return {"session_id": inv.id, "location": inv.location}

    @app.post("/rfid/inventory/{sid}/scan")
    def inv_scan(
        sid: int, body: ScanIn, _: Principal = Depends(need("rfid.use")), session: Session = Depends(get_session)
    ):
        inv = session.get(rfid.InventorySession, sid) or _missing("Inventory session", sid)
        uids = body.uids + (app.state.hub.handheld.read_batch() if body.from_device else [])
        return {"added": rfid.record_scans(session, inv, uids), "total": len(inv.scanned)}

    @app.post("/rfid/inventory/{sid}/finish")
    def inv_finish(sid: int, user: Principal = Depends(need("rfid.use")), session: Session = Depends(get_session)):
        inv = session.get(rfid.InventorySession, sid) or _missing("Inventory session", sid)
        return rfid.finish_inventory(session, inv, actor=user.username)

    @app.post("/rfid/gate/simulate")
    def gate_simulate(body: GateWalkIn, _: Principal = Depends(need("rfid.use"))):
        """Demo/test hook: drives the mock gate. Real gates publish to the bus themselves."""
        app.state.hub.gate.walk_through(body.uids, body.people)
        return {"published": len(body.uids), "spooled": len(app.state.hub.bus.spool)}

    # ----- import -----
    def _save_upload(file: UploadFile) -> Path:
        updir = rt.settings.data_dir / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            raise DomainError("UNSUPPORTED_FILE", "Upload a .csv or .xlsx file")
        target = updir / f"{uuid.uuid4().hex}{suffix}"
        with target.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        return target

    @app.post("/imports/dry-run")
    def import_dry(
        file: UploadFile = File(...),
        user: Principal = Depends(need("import.run")),
        session: Session = Depends(get_session),
    ):
        return migration.run_import(session, rt.settings, _save_upload(file), dry_run=True, actor=user.username)

    @app.post("/imports/load")
    def import_load(
        file: UploadFile = File(...),
        user: Principal = Depends(need("import.run")),
        session: Session = Depends(get_session),
    ):
        return migration.run_import(session, rt.settings, _save_upload(file), dry_run=False, actor=user.username)

    @app.post("/imports/{batch_id}/rollback")
    def import_rollback(
        batch_id: int, user: Principal = Depends(need("import.run")), session: Session = Depends(get_session)
    ):
        return migration.rollback_import(session, batch_id, actor=user.username)

    # ----- protocols -----
    @app.post("/integrations/ncip")
    async def api_ncip(request: Request, user: Principal = Depends(need("integration.use"))):
        body = await request.body()
        with rt.factory() as session:
            try:
                out = ncip.handle(body, session, rt.settings, app.state.hub, actor=user.username)
                session.commit()
            except Exception:
                session.rollback()
                raise
        return Response(content=out, media_type="application/xml")

    # ----- reports -----
    @app.get("/reports/dashboard")
    def api_dashboard(
        start: datetime | None = None,
        end: datetime | None = None,
        location: str | None = None,
        _: Principal = Depends(need("reports.read")),
        session: Session = Depends(get_session),
    ):
        return reports.dashboard(session, start=start, end=end, location=location)

    @app.get("/reports/circulation.csv")
    def api_circ_csv(
        start: datetime | None = None,
        end: datetime | None = None,
        location: str | None = None,
        _: Principal = Depends(need("reports.read")),
        session: Session = Depends(get_session),
    ):
        return Response(
            reports.circulation_csv(session, start=start, end=end, location=location), media_type="text/csv"
        )

    # ----- admin -----
    @app.get("/admin/audit")
    def api_audit(
        limit: int = 50,
        action: str | None = None,
        _: Principal = Depends(need("audit.read")),
        session: Session = Depends(get_session),
    ):
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
        if action:
            stmt = stmt.where(AuditLog.action == action)
        return [
            {
                "at": a.at.isoformat(),
                "actor": a.actor,
                "action": a.action,
                "entity": a.entity,
                "entity_id": a.entity_id,
                "detail": a.detail,
            }
            for a in session.scalars(stmt)
        ]

    @app.post("/admin/users", status_code=201)
    def api_create_user(
        body: UserIn, user: Principal = Depends(need("users.manage")), session: Session = Depends(get_session)
    ):
        if session.scalar(select(User).where(User.username == body.username)):
            raise DomainError("USER_EXISTS", "Username already exists", status=409)
        session.add(
            User(
                username=body.username,
                role=body.role,
                password_hash=hash_password(body.password),
                card_uid=body.card_uid,
            )
        )
        audit(
            session, user.username, "user.create", "user", body.username, {"role": body.role, "card_uid": body.card_uid}
        )
        return {"username": body.username, "role": body.role}

    @app.post("/admin/notifications/dispatch")
    def api_dispatch(_: Principal = Depends(need("notifications.run")), session: Session = Depends(get_session)):
        return notif.dispatch(session, app.state.providers)

    @app.post("/admin/backup")
    def api_backup(user: Principal = Depends(need("backup.run")), session: Session = Depends(get_session)):
        path = backup_ops.create_backup(rt.settings.db_path, rt.settings.backups_dir, label="api")
        audit(session, user.username, "backup.create", "database", path.name)
        return {"file": path.name}

    return app


def _missing(what: str, ident) -> None:
    raise NotFound("NOT_FOUND", f"{what} {ident} not found")
