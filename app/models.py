"""Our own schema. It sits beside the ILMS and links to it only by stable external keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db import utcnow


class Base(DeclarativeBase):
    pass


class ImportBatch(Base):
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="staging")  # dry_run|loaded|rolled_back|failed
    actor: Mapped[str] = mapped_column(String(80), default="system")
    report: Mapped[dict | None] = mapped_column(JSON)


class StgImport(Base):
    __tablename__ = "stg_import"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12))  # valid|rejected|duplicate
    reason: Mapped[str | None] = mapped_column(String(60))


class Bib(Base):
    __tablename__ = "bib"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(300))
    isbn: Mapped[str | None] = mapped_column(String(20), index=True)
    publisher: Mapped[str | None] = mapped_column(String(300))
    year: Mapped[int | None] = mapped_column(Integer)
    source_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"), index=True)
    items: Mapped[list[Item]] = relationship(back_populates="bib")


class Item(Base):
    __tablename__ = "item"
    id: Mapped[int] = mapped_column(primary_key=True)
    bib_id: Mapped[int] = mapped_column(ForeignKey("bib.id"), index=True)
    accession_no: Mapped[str] = mapped_column(String(40), unique=True)
    barcode: Mapped[str] = mapped_column(String(40), unique=True)
    location: Mapped[str] = mapped_column(String(80), index=True)
    is_reference: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="available")  # available|on_loan|lost
    source_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"), index=True)
    bib: Mapped[Bib] = relationship(back_populates="items")
    tags: Mapped[list[RfidTag]] = relationship(back_populates="item")


class RfidTag(Base):
    __tablename__ = "rfid_tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(40), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active|retired
    secured: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    item: Mapped[Item] = relationship(back_populates="tags")
    __table_args__ = (
        # A tag UID may be active on only one item at a time. Retired rows keep history.
        Index("ux_active_tag_uid", "uid", unique=True, sqlite_where=text("status = 'active'")),
    )


class Member(Base):
    __tablename__ = "member"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_no: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(30))
    card_uid: Mapped[str | None] = mapped_column(String(40), unique=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    fines_due: Mapped[float] = mapped_column(Float, default=0.0)
    notify_channel: Mapped[str] = mapped_column(String(10), default="email")  # email|sms|print


class User(Base):
    """Staff operators."""

    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    role: Mapped[str] = mapped_column(String(20))  # admin|librarian|assistant|integration
    password_hash: Mapped[str | None] = mapped_column(String(300))
    card_uid: Mapped[str | None] = mapped_column(String(40), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Loan(Base):
    __tablename__ = "loan"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    out_at: Mapped[datetime] = mapped_column(DateTime)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime)
    renewals: Mapped[int] = mapped_column(Integer, default=0)
    fine_charged: Mapped[float] = mapped_column(Float, default=0.0)
    item: Mapped[Item] = relationship()
    member: Mapped[Member] = relationship()
    __table_args__ = (
        Index("ux_one_open_loan_per_item", "item_id", unique=True, sqlite_where=text("returned_at IS NULL")),
    )


class LoanEvent(Base):
    """Append-only transaction history."""

    __tablename__ = "loan_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loan.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # checkout|checkin|renew
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(10), default="ui")  # ui|ncip|sip2
    actor: Mapped[str] = mapped_column(String(80), default="system")


class InventorySession(Base):
    __tablename__ = "inventory_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    location: Mapped[str] = mapped_column(String(80))
    device_id: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default="open")
    scanned: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict | None] = mapped_column(JSON)


class GateEvent(Base):
    __tablename__ = "gate_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(12))  # alarm|pass|footfall
    device_id: Mapped[str] = mapped_column(String(60))
    location: Mapped[str | None] = mapped_column(String(80))
    tag_uid: Mapped[str | None] = mapped_column(String(40))
    accession_no: Mapped[str | None] = mapped_column(String(40))
    image_path: Mapped[str | None] = mapped_column(String(300))
    count: Mapped[int] = mapped_column(Integer, default=1)


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(10))
    to: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class Device(Base):
    __tablename__ = "device"
    device_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40), index=True)
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    detail: Mapped[dict | None] = mapped_column(JSON)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
