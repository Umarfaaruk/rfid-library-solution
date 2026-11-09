"""Queued notifications with retry. One provider adapter per channel (FR 09)."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Member, Notification


class Provider(Protocol):
    def send(self, to: str, subject: str, body: str, attachments: list[str]) -> None: ...


class MockProvider:
    """Records what would have been sent. fail_next(n) simulates provider outages."""

    def __init__(self, channel: str):
        self.channel = channel
        self.outbox: list[dict] = []
        self._fail = 0

    def fail_next(self, n: int = 1) -> None:
        self._fail = n

    def send(self, to: str, subject: str, body: str, attachments: list[str]) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise ConnectionError(f"{self.channel} provider unavailable")
        self.outbox.append({"to": to, "subject": subject, "body": body, "attachments": attachments})


def mock_providers() -> dict[str, MockProvider]:
    return {c: MockProvider(c) for c in ("email", "sms", "print")}


def enqueue(
    session: Session, channel: str, to: str, subject: str, body: str, attachments: list[str] | None = None
) -> Notification:
    n = Notification(channel=channel, to=to, subject=subject, body=body, attachments=attachments or [])
    session.add(n)
    session.flush()
    return n


def notify_member(session: Session, member: Member, subject: str, body: str) -> Notification | None:
    """Send on the member's chosen channel. Falls back to email if the chosen one has no address."""
    channel = member.notify_channel
    address = {"email": member.email, "sms": member.phone, "print": member.member_no}.get(channel)
    if not address and member.email:
        channel, address = "email", member.email
    if not address:
        return None
    return enqueue(session, channel, address, subject, body)


def dispatch(session: Session, providers: dict[str, Provider], *, max_attempts: int = 3) -> dict:
    sent = failed = retried = 0
    pending = session.scalars(
        select(Notification).where(Notification.status == "pending").order_by(Notification.id)
    ).all()
    for n in pending:
        provider = providers.get(n.channel)
        n.attempts += 1
        try:
            if provider is None:
                raise LookupError(f"no provider for channel {n.channel}")
            provider.send(n.to, n.subject, n.body, n.attachments)
            n.status, n.sent_at, n.last_error = "sent", utcnow(), None
            sent += 1
        except Exception as exc:
            n.last_error = str(exc)[:300]
            if n.attempts >= max_attempts:
                n.status = "failed"
                failed += 1
            else:
                retried += 1
    return {"sent": sent, "failed": failed, "will_retry": retried}
