"""Append-only audit trail with masking of personal values."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AuditLog

_MASK_KEYS = {"password", "token", "card_uid", "email", "phone", "secret"}


def _mask(value: str) -> str:
    return "***" if len(value) <= 4 else f"{value[:2]}***{value[-2:]}"


def mask_detail(detail: dict | None) -> dict:
    out: dict = {}
    for key, val in (detail or {}).items():
        if key.lower() in _MASK_KEYS and isinstance(val, str):
            out[key] = _mask(val)
        elif isinstance(val, dict):
            out[key] = mask_detail(val)
        else:
            out[key] = val
    return out


def audit(
    session: Session,
    actor: str,
    action: str,
    entity: str,
    entity_id: object | None = None,
    detail: dict | None = None,
    *,
    now: datetime | None = None,
) -> None:
    session.add(
        AuditLog(
            at=now or utcnow(),
            actor=actor,
            action=action,
            entity=entity,
            entity_id=None if entity_id is None else str(entity_id),
            detail=mask_detail(detail),
        )
    )
