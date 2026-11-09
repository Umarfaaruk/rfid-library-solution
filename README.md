# RFID Library Solution (prototype)

A working prototype of an RFID-enabled library layer that sits beside an existing ILMS. It was built for the AISYS
technical evaluation (SOP sections 1 to 10). All devices, the ILMS and all providers are mocked behind documented
interfaces. All data is synthetic.

## What works

| Area | What you can run |
|---|---|
| Web console | Public catalogue search, staff sign-in (password or mock smart card), circulation desk, RFID tools, dashboard, admin |
| Circulation | Check out, check in, renew, fines, blocks, reference restriction, loan limit, audit trail |
| RFID middleware | Mock staff reader, handheld, gate, camera, printer, card reader; event bus with retries, spool and replay |
| Interoperability | NCIP 2.0 subset (HTTP, XML) and SIP2 subset (TCP) over the same rules |
| Migration | Stage, validate, dedupe, dry-run, load, reconcile, roll back; CSV and XLSX; about 20,000 rows in about 1 second |
| Operations | Backup, restore, offline installer, offline update with automatic rollback, health check |
| Security | scrypt passwords, signed tokens, server-side role matrix, masked audit log, safe XML parsing |

Acceptance scenarios AC01 to AC10 each have an automated test. See `docs/requirements_traceability.csv`.

## Quick start (any machine with Python 3.12 or later)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.lock pytest pytest-cov httpx ruff
export RFIDLIB_DATA_DIR=./data                          # Windows: set RFIDLIB_DATA_DIR=data

python -m app.cli init-db
python -m app.cli seed-demo
python scripts/generate_synthetic_data.py --rows 20000 --out data/synthetic_library.csv
python -m app.cli import data/synthetic_library.csv --dry-run     # validate only
python -m app.cli import data/synthetic_library.csv               # load
python -m app.cli serve                                           # http://127.0.0.1:8000
```

Demo accounts (synthetic): `admin / Admin#2026`, `librarian / Libra#2026`, `assistant / Assist#2026`,
`selfcheck / Kiosk#2026` (integration role). Smart-card mock UIDs: `CARD-ADMIN`, `CARD-LIB`, `CARD-ASST`.
Demo members: `M0001` to `M0004`.

Other commands: `python -m app.cli health | version | backup | restore FILE | rollback BATCH_ID | sip2`.
API reference is generated at `http://127.0.0.1:8000/docs`.

## Tests

```bash
ruff check . && ruff format --check .
pytest --cov=app
python scripts/measure_performance.py
python scripts/verify_offline_lifecycle.py     # builds 3 packages, installs with no network, updates, rolls back
```

Last recorded results are in `docs/evidence/` (97 tests passing, 95 percent line coverage).

## Layout

```
app/            application (layers: static UI, main.py API, services, rfid middleware, adapters, ops, models)
app/static/     staff console and OPAC (plain HTML, CSS, JS; no build step, no CDN, works offline)
scripts/        data generator, offline package builder, lifecycle verifier, performance measurement
packaging/      install.py, run.py, update.py that ship inside the offline package
tests/          one file per acceptance scenario plus CLI and UI tests
docs/           requirements, architecture, admin guide, user guide, test package, limitations, evidence
```

## Documents

1. `docs/01_REQUIREMENTS.md` assumptions, clarifications, use cases, data dictionary
2. `docs/02_ARCHITECTURE.md` context, components, deployment, database, interfaces, security, decisions
3. `docs/03_ADMIN_GUIDE.md` install, configuration, devices, backup, update, rollback, runbook, licences
4. `docs/04_USER_GUIDE.md` user procedures and training plan
5. `docs/05_TEST_PACKAGE.md` test plan, manual scenarios, defect log, performance, security, UAT
6. `docs/06_LIMITATIONS_AND_BACKLOG.md` honest limits and production-completion backlog
7. `docs/07_DEMO_SCRIPT.md` 6 minute demonstration script
8. `docs/requirements_traceability.csv` requirement to code to test matrix

## Honest limits

No real RFID hardware, ILMS or provider was used. NCIP and SIP2 are tested subsets with no conformance or
certification claim. The demo database is SQLite. Acquisition, serials, spine labels, net cataloguing and a virtual
bookshelf are not built. See `docs/06_LIMITATIONS_AND_BACKLOG.md`.
