"""Runtime settings. Everything tunable lives here or in the environment, never in business code."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from app import __version__


def load_or_create_secret(data_dir: Path) -> str:
    """Per-installation signing key, generated once and kept next to the data (owner-readable only)."""
    key_file = data_dir / "jwt.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    key_file.write_text(secret, encoding="utf-8")
    key_file.chmod(0o600)
    return secret


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    db_file: str = "library.db"
    app_version: str = __version__

    # Circulation policy (FR 04): configuration, not code.
    loan_days: int = 14
    max_renewals: int = 2
    max_loans: int = 5
    fine_per_day: float = 2.0
    fine_limit: float = 50.0

    # RFID data model. AFI values follow the ISO 28560 convention: C2 = secured, 07 = unsecured.
    isil: str = "IN-DEMO-001"
    afi_secured: int = 0xC2
    afi_unsecured: int = 0x07
    gate_dedupe_seconds: int = 10
    security_email: str = "security@library.example"

    # Auth
    jwt_secret: str = "dev-only-change-me"  # noqa: S105  (overridden by env in any real run)
    jwt_minutes: int = 60

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_file

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def gate_images_dir(self) -> Path:
        return self.data_dir / "gate_images"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def with_(self, **changes) -> Settings:
        return replace(self, **changes)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        base = cls()
        data_dir = Path(env("RFIDLIB_DATA_DIR", str(base.data_dir)))
        secret = env("RFIDLIB_JWT_SECRET") or load_or_create_secret(data_dir)
        return replace(
            base,
            data_dir=data_dir,
            db_file=env("RFIDLIB_DB_FILE", base.db_file),
            loan_days=int(env("RFIDLIB_LOAN_DAYS", base.loan_days)),
            max_renewals=int(env("RFIDLIB_MAX_RENEWALS", base.max_renewals)),
            max_loans=int(env("RFIDLIB_MAX_LOANS", base.max_loans)),
            fine_per_day=float(env("RFIDLIB_FINE_PER_DAY", base.fine_per_day)),
            fine_limit=float(env("RFIDLIB_FINE_LIMIT", base.fine_limit)),
            isil=env("RFIDLIB_ISIL", base.isil),
            security_email=env("RFIDLIB_SECURITY_EMAIL", base.security_email),
            jwt_secret=secret,
        )
