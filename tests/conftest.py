from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import Runtime, build_runtime
from app.config import Settings
from app.main import create_app
from app.rfid import service as rfid
from app.seed import DEMO_USERS, seed_demo
from app.services.migration import COLUMNS, run_import

PASSWORDS = {u: p for u, _r, p, _c in DEMO_USERS}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", jwt_secret="test-secret-key-0123456789abcdef")


@pytest.fixture
def rt(settings: Settings):
    runtime = build_runtime(settings)
    with runtime.factory() as s:
        seed_demo(s)
        s.commit()
    yield runtime
    runtime.engine.dispose()


@pytest.fixture
def app(rt: Runtime):
    return create_app(rt)


@pytest.fixture
def hub(app):
    return app.state.hub


@pytest.fixture
def client(app):
    return TestClient(app)


def write_catalog(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return path


def make_rows(specs: list[tuple[str, str, bool]]) -> list[dict]:
    return [
        {
            "Accession No": acc,
            "Title": f"Title of {acc}",
            "Author": "A. Writer",
            "Location": loc,
            "Category": "Reference" if ref else "Lending",
            "Barcode": f"BC-{acc}",
        }
        for acc, loc, ref in specs
    ]


@pytest.fixture
def catalog(rt: Runtime, tmp_path: Path) -> list[str]:
    """12 items: ACC001-006 MAIN-A1, ACC007-010 MAIN-B1, ACC011-012 REF-1 (reference)."""
    specs = (
        [(f"ACC{i:03d}", "MAIN-A1", False) for i in range(1, 7)]
        + [(f"ACC{i:03d}", "MAIN-B1", False) for i in range(7, 11)]
        + [(f"ACC{i:03d}", "REF-1", True) for i in range(11, 13)]
    )
    path = write_catalog(tmp_path / "catalog.csv", make_rows(specs))
    with rt.factory() as s:
        run_import(s, rt.settings, path, dry_run=False, actor="test")
        s.commit()
    return [a for a, _l, _r in specs]


@pytest.fixture
def tagged(rt: Runtime, hub, catalog: list[str]) -> list[str]:
    """Tag every catalogue item with uid TAG-<accession>."""
    with rt.factory() as s:
        for acc in catalog:
            rfid.tag_item(s, rt.settings, hub, item_key=acc, actor="test", tag_uid=f"TAG-{acc}")
        s.commit()
    return catalog


def login(client: TestClient, username: str) -> dict[str, str]:
    r = client.post("/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin(client):
    return login(client, "admin")


@pytest.fixture
def librarian(client):
    return login(client, "librarian")


@pytest.fixture
def assistant(client):
    return login(client, "assistant")


@pytest.fixture
def kiosk(client):
    return login(client, "selfcheck")
