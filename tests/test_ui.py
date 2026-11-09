"""Web console and public OPAC (FR 02): served without a login, no private data exposed."""

from __future__ import annotations


def test_console_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "RFID Library" in page.text and "/ui/app.js" in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/ui/secret.py").status_code == 404
    assert client.get("/ui/..%2Fmain.py").status_code in (404, 422)


def test_opac_search_is_public_and_read_only(client, catalog):
    r = client.get("/opac/search?q=Title of ACC00")
    assert r.status_code == 200 and len(r.json()["results"]) >= 9
    body = r.text
    assert "member" not in body.lower() and "borrower" not in body.lower()
    assert client.get("/opac/search?q=%22%3B+DROP+TABLE+bib%3B--").json()["results"] == []
    assert client.get("/opac/search?q=Title&limit=9999").status_code == 200


def test_opac_is_not_a_back_door_to_staff_endpoints(client, catalog):
    assert client.get("/catalog/items/ACC001").status_code == 401
    assert client.get("/reports/dashboard").status_code == 401
