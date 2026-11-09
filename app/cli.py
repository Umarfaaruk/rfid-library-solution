"""Command line for operators: python -m app.cli <command>. Works fully offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

from app import __version__
from app.bootstrap import build_runtime
from app.config import Settings
from app.ops import backup as backup_ops
from app.ops.schema import LATEST, current_version
from app.seed import seed_demo
from app.services import migration


def health(settings: Settings) -> dict:
    rt = build_runtime(settings, migrate_schema=False)
    try:
        with rt.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        version = current_version(rt.engine)
        return {
            "status": "ok" if version == LATEST else "schema_outdated",
            "app_version": __version__,
            "schema_version": version,
            "schema_expected": LATEST,
        }
    finally:
        rt.engine.dispose()


def run_sip2(settings: Settings, host: str, port: int) -> int:
    import asyncio

    from app.adapters import sip2
    from app.rfid.devices import mock_hub

    rt = build_runtime(settings)
    hub = mock_hub(afi_secured=settings.afi_secured, afi_unsecured=settings.afi_unsecured)

    async def go() -> None:
        server = await sip2.serve(lambda: sip2.Sip2Handler(rt.factory, settings, hub), host, port)
        print(f"SIP2 listening on {host}:{port}", flush=True)
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        rt.engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rfidlib")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create or upgrade the schema")
    sub.add_parser("seed-demo", help="add synthetic demo users and members")
    sub.add_parser("health", help="print a health report")
    sub.add_parser("version", help="print versions")
    imp = sub.add_parser("import", help="import a .csv/.xlsx catalogue")
    imp.add_argument("file", type=Path)
    imp.add_argument("--dry-run", action="store_true")
    rb = sub.add_parser("rollback", help="roll back an import batch")
    rb.add_argument("batch_id", type=int)
    bk = sub.add_parser("backup", help="create a database backup")
    bk.add_argument("--label", default="manual")
    rs = sub.add_parser("restore", help="restore a backup file over the live database")
    rs.add_argument("file", type=Path)
    srv = sub.add_parser("serve", help="run the HTTP API")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    sp = sub.add_parser("sip2", help="run the SIP2 TCP listener (mock self-check / sorter clients)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=6001)
    args = p.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "version":
        print(json.dumps({"app": __version__, "schema_expected": LATEST}))
        return 0
    if args.cmd == "health":
        report = health(settings)
        print(json.dumps(report))
        return 0 if report["status"] == "ok" else 1
    if args.cmd == "restore":
        backup_ops.restore_backup(args.file, settings.db_path)
        print(f"restored {args.file} -> {settings.db_path}")
        return 0
    if args.cmd == "serve":
        import uvicorn

        from app.main import create_app

        uvicorn.run(create_app(build_runtime(settings)), host=args.host, port=args.port)
        return 0

    if args.cmd == "sip2":
        return run_sip2(settings, args.host, args.port)

    rt = build_runtime(settings)
    try:
        if args.cmd == "init-db":
            print(json.dumps({"schema_version": current_version(rt.engine)}))
        elif args.cmd == "seed-demo":
            with rt.factory() as s:
                print(json.dumps(seed_demo(s)))
                s.commit()
        elif args.cmd == "import":
            with rt.factory() as s:
                report = migration.run_import(s, settings, args.file, dry_run=args.dry_run, actor="cli")
                s.commit()
            print(json.dumps(report, indent=2))
            return 0 if report["balanced"] else 2
        elif args.cmd == "rollback":
            with rt.factory() as s:
                print(json.dumps(migration.rollback_import(s, args.batch_id, actor="cli")))
                s.commit()
        elif args.cmd == "backup":
            path = backup_ops.create_backup(settings.db_path, settings.backups_dir, label=args.label)
            print(path)
    finally:
        rt.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
