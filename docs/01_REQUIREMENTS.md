# D1 Requirements package

## 1. Assumptions

| # | Assumption | Why it matters |
|---|---|---|
| A1 | The existing ILMS owns acquisition, serials and spine-label printing. This solution adds RFID workflows beside it. | Explains why FR01 and FR02 are partly built |
| A2 | The ILMS database is not available. The ILMS is represented by this application's own catalogue plus NCIP and SIP2 adapters. | SOP section 1: define an adapter and supply a mock |
| A3 | "About 20,000 records" means a spreadsheet with a header row and roughly that many data rows, with realistic defects. | Drives the synthetic dataset |
| A4 | Tags follow an ISO 28560 style layout (item identifier, ISIL, AFI secured or unsecured). Only the fields used here are modelled. | Keeps the tag model honest |
| A5 | One active tag per item. Re-tagging retires the old tag and keeps history. | Simplifies the gate rule |
| A6 | Circulation policy (loan days, renewals, loan limit, fine per day, fine limit) is one global setting. | Per-category rules are backlog |
| A7 | Fines are a simple per-day amount, recorded when an overdue item is returned. | Enough for the fine-limit rule |
| A8 | The demo runs on SQLite. A multi-user production site would use a server database. | See limitations |
| A9 | All data and credentials are synthetic and the solution never contacts a live service. | SOP stage 1 |

## 2. Clarification log

The SOP could not be discussed with its authors, so each open question was answered by choosing the safer reading and recording it.

| # | Question | Decision taken | Owner to confirm |
|---|---|---|---|
| Q1 | Which ILMS and which version? | Unknown. NCIP 2.0 and SIP2 chosen because the SOP names them | AISYS |
| Q2 | Should the gate decide alone or ask the ILMS? | Gate decides from local data so it still works when the ILMS is offline (FR07) | AISYS |
| Q3 | What counts as a unique record on import? | Accession number and barcode must each be unique | Library |
| Q4 | Are duplicates errors or just skipped? | Reported separately from rejected rows and never loaded | Library |
| Q5 | Is a reference item ever lendable? | No. Blocked at check-out | Library |
| Q6 | Which fine limit? | Configurable, default 50.00 | Library |
| Q7 | Is encrypted transport inside scope? | Expected from a reverse proxy in front of the app. Not terminated by the app | AISYS IT |
| Q8 | Windows service or console process? | Console process in the prototype. Service wrapper is backlog | AISYS IT |

## 3. Use cases

| ID | Actor | Use case | Main flow | Covered by |
|---|---|---|---|---|
| UC1 | Librarian | Migrate a catalogue | Dry-run, read the report, load, reconcile, roll back if wrong | AC01 |
| UC2 | Librarian | Tag an item | Enter item, validate, write tag, see tag-to-item link | AC02 |
| UC3 | Assistant | Lend and return | Scan member, scan item, system applies rules, security bit flips | AC03, AC04 |
| UC4 | Self-check kiosk | Lend through SIP2 or NCIP | Login, patron status, checkout, checkin | AC03 |
| UC5 | Librarian | Verify a shelf | Start inventory, scan, finish, read missing and misplaced | AC05 |
| UC6 | Security gate | Detect unauthorised removal | Tag read, rule check, alarm, photo, email | AC06 |
| UC7 | Any staff | Sign in with a card | Tap card, receive role-scoped token | AC07 |
| UC8 | Library manager | Read statistics | Open dashboard, filter by date or location, export CSV | AC08 |
| UC9 | Administrator | Install, update and roll back offline | Install from package, apply update, automatic rollback on failure | AC09 |
| UC10 | Administrator | Recover from a bad change | Restore backup, compare fingerprints | AC10 |
| UC11 | Public | Search the catalogue | Type words, see titles and availability | FR02 |

## 4. Data dictionary (core tables)

| Table | Purpose | Columns |
|---|---|---|
| bib | Bibliographic record | id, title, author, isbn, publisher, year, source_batch_id |
| item | Physical copy | id, bib_id, accession_no (unique), barcode (unique), location, is_reference, status, source_batch_id |
| rfid_tag | Tag linked to an item | id, uid, item_id, status (active or retired), secured, created_at, retired_at. One active row per item and per uid (partial unique index) |
| member | Library member | id, member_no (unique), name, email, phone, card_uid, blocked, fines_due, notify_channel |
| app_user | Staff or integration account | id, username (unique), role, password_hash (scrypt), card_uid, active |
| loan | One borrowing | id, item_id, member_id, out_at, due_at, returned_at, renewals, fine_charged. A partial unique index allows one open loan per item |
| loan_event | Audit of each loan action | id, loan_id, kind (checkout, renew, checkin), at, source (ui, ncip, sip2), actor |
| inventory_session | Shelf check | id, location, device_id, actor, started_at, finished_at, status, scanned, result |
| gate_event | Gate activity | id, at, kind (alarm, pass, footfall), device_id, location, tag_uid, accession_no, image_path, count |
| notification | Queued message | id, channel (email, sms, print), to, subject, body, attachments, status, attempts, last_error, created_at, sent_at |
| device | Registered RFID client | device_id, kind, last_seen |
| import_batch | One migration run | id, source_name, started_at, finished_at, status, actor, report |
| stg_import | Staging rows | id, batch_id, row_no, raw, status, reason |
| audit_log | Who did what | id, at, actor, action, entity, entity_id, detail (masked) |
| schema_version | Applied migrations | version, name, applied_at |
