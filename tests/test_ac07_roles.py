"""AC 07: smart-card login and role-based access, enforced server-side."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from app.db import utcnow
from app.errors import DomainError
from app.models import User
from app.security import hash_password, issue_token, read_token, verify_password
from tests.conftest import login


def test_card_login_issues_token_with_role(client):
    r = client.post("/auth/card-login", json={"card_uid": "CARD-LIB"})
    assert r.status_code == 200 and r.json()["role"] == "librarian"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/reports/dashboard", headers=headers).status_code == 200


def test_unknown_card_rejected_and_audited_with_masked_uid(client, admin):
    r = client.post("/auth/card-login", json={"card_uid": "STOLEN-CARD-99"})
    assert r.status_code == 401 and r.json()["code"] == "BAD_CREDENTIALS"
    rows = client.get("/admin/audit?action=auth.fail", headers=admin).json()
    assert rows and rows[0]["detail"]["card_uid"] == "ST***99"  # full UID never stored


def test_wrong_password_rejected_without_hint(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    r2 = client.post("/auth/login", json={"username": "nobody", "password": "wrong"})
    assert r.json() == r2.json()  # same answer for a bad user or a bad password


def test_permission_matrix(client, catalog, admin, librarian, assistant):
    csv_upload = {"file": ("c.csv", b"Accession No,Title,Location,Barcode\nZ1,T,L,B1\n", "text/csv")}
    # (method, path, kwargs, {role: expected_status})
    cases = [
        ("get", "/catalog/search?q=Title", {}, {"admin": 200, "librarian": 200, "assistant": 200}),
        (
            "post",
            "/circulation/checkout",
            {"json": {"member": "M0001", "item": "ACC001"}},
            {"admin": 200, "librarian": 200, "assistant": 200},
        ),
        (
            "post",
            "/rfid/inventory",
            {"json": {"location": "MAIN-A1"}},
            {"admin": 201, "librarian": 201, "assistant": 403},
        ),
        ("post", "/imports/dry-run", {"files": csv_upload}, {"admin": 200, "librarian": 200, "assistant": 403}),
        ("get", "/admin/audit", {}, {"admin": 200, "librarian": 403, "assistant": 403}),
        (
            "post",
            "/admin/users",
            {"json": {"username": "new", "role": "assistant", "password": "Password#123"}},
            {"admin": 201, "librarian": 403, "assistant": 403},
        ),
        ("post", "/admin/backup", {}, {"admin": 200, "librarian": 403, "assistant": 403}),
        (
            "post",
            "/members",
            {"json": {"member_no": "M9", "name": "N"}},
            {"admin": 201, "librarian": 201, "assistant": 403},
        ),
    ]
    tokens = {"admin": admin, "librarian": librarian, "assistant": assistant}
    for method, path, kwargs, expected in cases:
        for role, status in expected.items():
            if path == "/circulation/checkout":  # distinct item per role so the rule itself does not interfere
                kwargs = {
                    "json": {
                        "member": "M0001",
                        "item": {"admin": "ACC001", "librarian": "ACC002", "assistant": "ACC003"}[role],
                    }
                }
            if path == "/members":
                kwargs = {"json": {"member_no": f"M9{role}", "name": "N"}}
            if path == "/admin/users":
                kwargs = {"json": {"username": f"u-{role}", "role": "assistant", "password": "Password#123"}}
            if path == "/imports/dry-run":
                kwargs = {
                    "files": {
                        "file": (
                            "c.csv",
                            f"Accession No,Title,Location,Barcode\n{role},T,L,B{role}\n".encode(),
                            "text/csv",
                        )
                    }
                }
            r = getattr(client, method)(path, headers=tokens[role], **kwargs)
            assert r.status_code == status, f"{role} {method.upper()} {path} -> {r.status_code} {r.text}"


def test_missing_or_garbage_token_is_401(client):
    assert client.get("/reports/dashboard").status_code == 401
    assert client.get("/reports/dashboard", headers={"Authorization": "Bearer not.a.token"}).status_code == 401
    assert client.get("/reports/dashboard", headers={"Authorization": "Basic abc"}).status_code == 401


def test_tampered_and_expired_tokens_rejected(client, rt):
    good = login(client, "librarian")["Authorization"].split()[1]
    forged = jwt.encode(
        {"sub": "librarian", "role": "admin", "exp": utcnow() + timedelta(hours=1)},
        "wrong-secret-0123456789-0123456789-xx",
        algorithm="HS256",
    )
    assert client.get("/admin/audit", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode(
        {"sub": "admin", "role": "admin", "exp": utcnow() - timedelta(minutes=1)},
        rt.settings.jwt_secret,
        algorithm="HS256",
    )
    assert client.get("/admin/audit", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    none_alg = jwt.encode({"sub": "admin", "role": "admin"}, key=None, algorithm="none")
    assert client.get("/admin/audit", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401
    assert read_token(good, rt.settings).role == "librarian"


def test_role_comes_from_token_signed_by_server_only(rt):
    user = User(username="x", role="assistant")
    tok = issue_token(user, rt.settings)
    assert read_token(tok, rt.settings).can("circulation.write")
    assert not read_token(tok, rt.settings).can("import.run")
    with pytest.raises(DomainError):
        read_token(tok + "x", rt.settings)


def test_password_hashing_is_salted_and_verifiable():
    a, b = hash_password("Secret#123"), hash_password("Secret#123")
    assert a != b and a.startswith("scrypt$")
    assert verify_password("Secret#123", a) and not verify_password("secret#123", a)
    assert not verify_password("anything", None) and not verify_password("anything", "plaintext")


def test_deactivated_user_cannot_log_in(client, rt):
    with rt.factory() as s:
        s.query(User).filter_by(username="assistant").one().active = False
        s.commit()
    assert client.post("/auth/card-login", json={"card_uid": "CARD-ASST"}).status_code == 401


def test_audit_trail_records_login_and_actions(client, catalog, librarian, admin):
    client.post("/circulation/checkout", json={"member": "M0001", "item": "ACC001"}, headers=librarian)
    actions = [r["action"] for r in client.get("/admin/audit?limit=20", headers=admin).json()]
    assert "loan.checkout" in actions and "auth.login" in actions
