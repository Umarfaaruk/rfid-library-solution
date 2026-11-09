"""Versioned, forward-only schema migrations with a recorded history.

The runner is intentionally tiny so it works offline. Each migration runs in one transaction.
In production the same contract can be handed to Alembic without touching callers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import Connection, Engine, inspect, text

from app.models import Base

log = logging.getLogger(__name__)
Migration = tuple[int, str, Callable[[Connection], None]]


def _v1_core_tables(conn: Connection) -> None:
    Base.metadata.create_all(conn)


def _v2_search_index(conn: Connection) -> None:
    """FTS5 index over bib with triggers. Search code falls back to LIKE if FTS5 is missing."""
    conn.execute(
        text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS bib_fts USING fts5("
            "title, author, isbn, content='bib', content_rowid='id')"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS bib_ai AFTER INSERT ON bib BEGIN "
            "INSERT INTO bib_fts(rowid,title,author,isbn) VALUES (new.id,new.title,new.author,new.isbn); END"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS bib_ad AFTER DELETE ON bib BEGIN "
            "INSERT INTO bib_fts(bib_fts,rowid,title,author,isbn) "
            "VALUES ('delete',old.id,old.title,old.author,old.isbn); END"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS bib_au AFTER UPDATE ON bib BEGIN "
            "INSERT INTO bib_fts(bib_fts,rowid,title,author,isbn) "
            "VALUES ('delete',old.id,old.title,old.author,old.isbn); "
            "INSERT INTO bib_fts(rowid,title,author,isbn) VALUES (new.id,new.title,new.author,new.isbn); END"
        )
    )


def _v3_loan_indexes(conn: Connection) -> None:
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_loan_open_due ON loan(due_at) WHERE returned_at IS NULL"))


MIGRATIONS: list[Migration] = [
    (1, "core tables", _v1_core_tables),
    (2, "catalogue search index", _v2_search_index),
    (3, "open-loan due index", _v3_loan_indexes),
]
LATEST = MIGRATIONS[-1][0]


def current_version(engine: Engine) -> int:
    if not inspect(engine).has_table("schema_version"):
        return 0
    with engine.connect() as conn:
        return conn.execute(text("SELECT COALESCE(MAX(version),0) FROM schema_version")).scalar_one()


def migrate(engine: Engine, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order. Each runs in its own transaction; a failure stops the run."""
    plan = migrations if migrations is not None else MIGRATIONS
    have = current_version(engine)
    applied: list[int] = []
    for version, name, fn in sorted(plan, key=lambda m: m[0]):
        if version <= have:
            continue
        with engine.begin() as conn:
            fn(conn)
            if version == 1:
                Base.metadata.create_all(conn)  # makes sure schema_version exists before insert
            conn.execute(
                text("INSERT INTO schema_version(version,name,applied_at) VALUES (:v,:n,CURRENT_TIMESTAMP)"),
                {"v": version, "n": name},
            )
        log.info("applied migration %s: %s", version, name)
        applied.append(version)
    return applied
