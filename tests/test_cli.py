"""Operator CLI: init, seed, health, import (dry-run and real), rollback, backup, restore, version."""

from __future__ import annotations

import json

import pytest

from app import cli
from tests.conftest import make_rows, write_catalog


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "clidata"
    monkeypatch.setenv("RFIDLIB_DATA_DIR", str(data))
    monkeypatch.setenv("RFIDLIB_JWT_SECRET", "cli-test-secret-0123456789abcdef0123")
    return data


def run(capsys, *argv) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_version(env, capsys):
    code, out = run(capsys, "version")
    assert code == 0 and "schema_expected" in json.loads(out)


def test_health_before_and_after_init(env, capsys):
    code, out = run(capsys, "init-db")
    assert code == 0 and json.loads(out)["schema_version"] >= 3
    code, out = run(capsys, "health")
    assert code == 0 and json.loads(out)["status"] == "ok"


def test_seed_demo_is_repeatable(env, capsys):
    run(capsys, "init-db")
    assert run(capsys, "seed-demo")[0] == 0
    assert run(capsys, "seed-demo")[0] == 0


def test_import_dry_run_then_real_then_rollback(env, tmp_path, capsys):
    run(capsys, "init-db")
    src = write_catalog(tmp_path / "c.csv", make_rows([("A1", "L1", False), ("A2", "L1", False)]))
    code, out = run(capsys, "import", str(src), "--dry-run")
    dry = json.loads(out)
    assert code == 0 and dry["balanced"] and dry["mode"] == "dry_run"

    code, out = run(capsys, "import", str(src))
    real = json.loads(out)
    assert code == 0 and real["balanced"] and real["loaded"] == 2

    code, out = run(capsys, "rollback", str(real["batch_id"]))
    assert code == 0


def test_backup_and_restore_round_trip(env, tmp_path, capsys):
    run(capsys, "init-db")
    src = write_catalog(tmp_path / "c.csv", make_rows([("A1", "L1", False)]))
    run(capsys, "import", str(src))
    code, out = run(capsys, "backup", "--label", "t")
    assert code == 0
    backup_file = out.strip()
    assert backup_file.endswith(".db") or "backup" in backup_file

    # damage the live database, then restore
    (env / "library.db").write_bytes(b"corrupt")
    code, out = run(capsys, "restore", backup_file)
    assert code == 0 and "restored" in out
    code, out = run(capsys, "health")
    assert code == 0 and json.loads(out)["status"] == "ok"


def test_unknown_command_is_rejected(env):
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


def test_sip2_command_listens_and_answers_login(env, capsys):
    """Start the real CLI listener in a thread, log in over TCP, then stop it."""
    import asyncio
    import socket
    import threading

    run(capsys, "init-db")
    run(capsys, "seed-demo")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    thread = threading.Thread(target=cli.main, args=(["sip2", "--port", str(port)],), daemon=True)
    thread.start()
    reply = b""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as c:
                c.sendall(b"9300CNselfcheck|COKiosk#2026|\r")
                reply = c.recv(200)
            break
        except OSError:
            asyncio.run(asyncio.sleep(0.1))
    assert reply.startswith(b"941")
