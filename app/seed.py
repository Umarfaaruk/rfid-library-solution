"""Demo seed data. Entirely synthetic. Passwords here are for the local demo only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Member, User
from app.security import hash_password

DEMO_USERS = [
    ("admin", "admin", "Admin#2026", "CARD-ADMIN"),
    ("librarian", "librarian", "Libra#2026", "CARD-LIB"),
    ("assistant", "assistant", "Assist#2026", "CARD-ASST"),
    ("selfcheck", "integration", "Kiosk#2026", None),
]
DEMO_MEMBERS = [
    ("M0001", "Asha Rao", "asha@example.test", "9000000001", "CARD-M1", "email"),
    ("M0002", "Ravi Khan", "ravi@example.test", "9000000002", "CARD-M2", "sms"),
    ("M0003", "Meera Iyer", "meera@example.test", "9000000003", "CARD-M3", "print"),
    ("M0004", "Imran Das", "imran@example.test", "9000000004", "CARD-M4", "email"),
]


def seed_demo(session: Session) -> dict:
    users = members = 0
    for username, role, password, card in DEMO_USERS:
        if session.scalar(select(User).where(User.username == username)) is None:
            session.add(User(username=username, role=role, password_hash=hash_password(password), card_uid=card))
            users += 1
    for no, name, email, phone, card, chan in DEMO_MEMBERS:
        if session.scalar(select(Member).where(Member.member_no == no)) is None:
            session.add(Member(member_no=no, name=name, email=email, phone=phone, card_uid=card, notify_channel=chan))
            members += 1
    return {"users_added": users, "members_added": members}
