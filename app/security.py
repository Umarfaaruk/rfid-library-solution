"""Authentication (password and smart card), JWT tokens and role permissions (AC 07)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.errors import DomainError, Forbidden
from app.models import User

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "librarian": {
        "catalog.read",
        "catalog.write",
        "circulation.read",
        "circulation.write",
        "rfid.use",
        "members.read",
        "members.write",
        "reports.read",
        "import.run",
    },
    "assistant": {"catalog.read", "circulation.read", "circulation.write", "members.read", "reports.read"},
    "integration": {"integration.use"},
}


@dataclass(frozen=True)
class Principal:
    username: str
    role: str

    def can(self, permission: str) -> bool:
        perms = ROLE_PERMISSIONS.get(self.role, set())
        return "*" in perms or permission in perms

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise Forbidden("FORBIDDEN", f"Role '{self.role}' lacks permission '{permission}'", status=403)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_b64, digest_b64 = stored.split("$")
    salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected)


def authenticate_password(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username, User.active.is_(True)))
    return user if user and verify_password(password, user.password_hash) else None


def authenticate_card(session: Session, card_uid: str) -> User | None:
    """Mock credential provider: a presented smart card UID maps to an active staff user."""
    return session.scalar(select(User).where(User.card_uid == card_uid, User.active.is_(True)))


def issue_token(user: User, settings: Settings) -> str:
    now = utcnow()
    claims = {"sub": user.username, "role": user.role, "iat": now, "exp": now + timedelta(minutes=settings.jwt_minutes)}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def read_token(token: str, settings: Settings) -> Principal:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise DomainError("UNAUTHENTICATED", "Invalid or expired token", status=401) from exc
    return Principal(username=claims["sub"], role=claims["role"])
