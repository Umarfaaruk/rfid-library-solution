# D9 Demonstration script (about 6 minutes)

Record the screen with the narration below. Say the words in your own voice and be ready to explain every step.

## Setup before recording (not on camera)

```bash
export RFIDLIB_DATA_DIR=./demo-data
python -m app.cli init-db && python -m app.cli seed-demo
python scripts/generate_synthetic_data.py --rows 20000 --out demo-data/source.csv
python -m app.cli serve        # leave running, open http://127.0.0.1:8000
```

## Script

| Time | On screen | Say |
|---|---|---|
| 0:00 | Title slide | "This is an RFID layer that sits beside an existing ILMS. Every device and the ILMS are mocks behind documented interfaces. All data is synthetic." |
| 0:20 | Terminal: `python -m app.cli import demo-data/source.csv --dry-run` | "First a dry run on about 20,000 rows. Nothing is written. It shows rejected rows, duplicates and a reconciliation: source equals loaded plus rejected plus duplicates." |
| 0:55 | Same command without `--dry-run`, then show `data/reports` | "Now the real load. A backup is taken first. The error log lists each bad row with its reason. If it is wrong, `rollback` removes only this batch." |
| 1:30 | Browser, OPAC search | "Anyone can search. Full text search, no login, no private data." |
| 1:50 | Sign in with `CARD-LIB` | "Smart card login with a mock card. The role comes from a signed token and every route checks it on the server." |
| 2:10 | RFID tools, tag an item | "Tagging validates the item, writes an ISO 28560 style payload and links tag to item. A second tag is refused unless we replace." |
| 2:40 | Circulation desk: lend, then try a reference item, then block a member and try again | "Rules: reference items, blocked members and the fine limit stop circulation. Each action is audited." |
| 3:20 | Gate: walk the lent item, then an unlent item | "Lent item passes. Unlent item raises an alarm, saves a camera image and queues an email. The gate decides from local data, so it works when the ILMS is down." |
| 3:55 | Inventory with one tag missing and one from another shelf | "Handheld inventory finds missing and misplaced items. High beep means complete, low beep means exceptions." |
| 4:25 | Dashboard, set location filter | "Dashboard for tagged items, members, circulation, operators, RFID clients, fines and gate events, filterable." |
| 4:45 | Terminal: `python scripts/verify_offline_lifecycle.py` (show the passing steps) | "Offline lifecycle with the network unreachable: install, update with migration, a deliberately broken update that rolls back automatically, identical database fingerprints, and a tampered package rejected." |
| 5:30 | `pytest` summary and the traceability CSV | "97 tests, each acceptance scenario AC01 to AC10 has automated proof, and every requirement maps to code and tests." |
| 5:50 | Limitations slide | "What is not done: real hardware and ILMS, protocol certification, a server database, TLS, acquisitions and serials. These are in the backlog." |

## Questions to be ready for

- Why SQLite? Demo only. Models are SQLAlchemy, so a server database is the production path, but it is untested here.
- Why not Alembic? One small runner that works offline. Alembic is a fair production choice.
- What does "fail closed" mean? If the security bit cannot be written, check-out is refused so the item cannot leave unprotected.
- How do you know the migration is right? The generator writes a truth file with the planted defects and the test compares the result to it.
- Is NCIP or SIP2 certified? No. Subsets tested against my own tests only.
- What would you do first with a real ILMS? Read-only integration in a test environment.
