"""Wire settings, engine, sessions and schema together. Used by the API, CLI and tests."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.ops.schema import migrate


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    factory: sessionmaker[Session]

    def session(self) -> Session:
        return self.factory()


def build_runtime(settings: Settings, *, migrate_schema: bool = True) -> Runtime:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    if migrate_schema:
        migrate(engine)
    return Runtime(settings, engine, make_session_factory(engine))
