"""Catalogue lookups and search. Search uses FTS5 when present and falls back to LIKE."""

from __future__ import annotations

import re

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.errors import NotFound
from app.models import Bib, Item, Member, RfidTag


def resolve_item(session: Session, key: str) -> Item:
    """Find an item by accession number, barcode or active tag UID."""
    key = (key or "").strip()
    item = session.scalar(select(Item).where(or_(Item.accession_no == key, Item.barcode == key)))
    if item is None:
        item = session.scalar(
            select(Item).join(RfidTag, RfidTag.item_id == Item.id).where(RfidTag.uid == key, RfidTag.status == "active")
        )
    if item is None:
        raise NotFound("ITEM_NOT_FOUND", f"No item matches '{key}'")
    return item


def resolve_member(session: Session, key: str) -> Member:
    key = (key or "").strip()
    member = session.scalar(select(Member).where(or_(Member.member_no == key, Member.card_uid == key)))
    if member is None:
        raise NotFound("MEMBER_NOT_FOUND", f"No member matches '{key}'")
    return member


def _fts_available(session: Session) -> bool:
    return bool(session.execute(text("SELECT 1 FROM sqlite_master WHERE name='bib_fts'")).first())


def search_catalog(session: Session, q: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", q or "") if w]
    if not words:
        return []
    if _fts_available(session):
        match = " AND ".join(f'"{w}"*' for w in words)
        rows = session.execute(
            text("SELECT rowid FROM bib_fts WHERE bib_fts MATCH :m ORDER BY rank LIMIT :l OFFSET :o"),
            {"m": match, "l": limit, "o": offset},
        ).all()
        ids = [r[0] for r in rows]
        bibs = {b.id: b for b in session.scalars(select(Bib).where(Bib.id.in_(ids)))} if ids else {}
        ordered = [bibs[i] for i in ids if i in bibs]
    else:
        stmt = select(Bib)
        for w in words:
            like = f"%{w}%"
            stmt = stmt.where(or_(Bib.title.like(like), Bib.author.like(like), Bib.isbn.like(like)))
        ordered = list(session.scalars(stmt.order_by(Bib.id).limit(limit).offset(offset)))
    out = []
    for bib in ordered:
        items = session.scalars(select(Item).where(Item.bib_id == bib.id)).all()
        out.append(
            {
                "bib_id": bib.id,
                "title": bib.title,
                "author": bib.author,
                "isbn": bib.isbn,
                "year": bib.year,
                "copies": [
                    {
                        "accession_no": i.accession_no,
                        "location": i.location,
                        "status": i.status,
                        "reference": i.is_reference,
                    }
                    for i in items
                ],
            }
        )
    return out
