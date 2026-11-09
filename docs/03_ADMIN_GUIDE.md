# D7 and D8 Administrator guide, deployment and runbook

## 1. Environment checklist

| Item | Requirement |
|---|---|
| Server | Windows Server 2022 or later, or Linux. 2 CPU, 4 GB RAM, 5 GB disk is ample for 20,000 items |
| Python | 3.12 or later on the server (the installer builds its own virtual environment from bundled wheels) |
| Clients | Windows 11 with a current browser |
| Network | Ethernet. No internet needed. Open TCP 8000 (web and NCIP) and 6001 (SIP2) from the LAN only |
| Time | Server clock correct. Due dates and fines depend on it |
| Accounts | A service account that owns the install folder |
| TLS | Terminate HTTPS in a reverse proxy in front of port 8000 |

## 2. Install from the offline package

Build (on any machine with internet, once per release):

```bash
python scripts/build_offline_package.py --version 1.0.0 --out dist            # Linux or macOS target
python scripts/build_offline_package.py --version 1.0.0 --out dist --windows  # Windows wheels
```

This produces `rfid-library-1.0.0-offline.zip` and a `.sha256` file. The package contains the code, every dependency as a wheel, `requirements.lock`, `SBOM.json`, a per-file SHA-256 `manifest.json`, and `install.py`, `run.py`, `update.py`.

Install (on the isolated server):

1. Copy the zip and the `.sha256` file by approved media. Compare the hash.
2. Unzip, then from the unzipped folder run `python install.py D:\rfid-library` (or a Linux path).
3. The installer verifies the manifest, creates a virtual environment from the bundled wheels (no network), creates the database and writes the first `current.json`.
4. Start: `python D:\rfid-library\run.py serve --port 8000`.
5. Check: `python D:\rfid-library\run.py health` should print `"status": "ok"`.
6. Create real users. The demo accounts must not be used in production (see section 8).

## 3. Configuration

All settings are environment variables with safe defaults. Nothing is hard-coded in business logic. A sample is in `config.sample.env`.

| Variable | Default | Meaning |
|---|---|---|
| RFIDLIB_DATA_DIR | data | Folder for database, backups, reports, gate images, jwt.key |
| RFIDLIB_DB_FILE | library.db | Database file name |
| RFIDLIB_JWT_SECRET | generated | Token signing secret. If unset, a random one is created once in `jwt.key` |
| RFIDLIB_LOAN_DAYS | 14 | Loan period |
| RFIDLIB_MAX_RENEWALS | 2 | Renewals per loan |
| RFIDLIB_MAX_LOANS | 5 | Items per member |
| RFIDLIB_FINE_PER_DAY | 2.0 | Fine per overdue day |
| RFIDLIB_FINE_LIMIT | 50.0 | Borrowing is blocked at or above this balance |
| RFIDLIB_ISIL | IN-DEMO-001 | Library identifier written to tags |
| RFIDLIB_SECURITY_EMAIL | security@library.example | Gate alarm recipient |

## 4. Device and interface configuration

The prototype uses mock devices. This table is the contract a real driver must meet.

| Device | Interface (Python Protocol in `app/rfid/devices.py`) | Address (to fill in on site) |
|---|---|---|
| Staff reader | read one tag, write tag data, set security bit | USB or IP: ________ |
| Handheld | read a batch, beep | Wi-Fi or dock: ________ |
| Security gate | publishes `gate.tag_read` events with tag uids and a people count | IP ________ port ________ |
| Card reader | returns a card uid | USB: ________ |
| Camera | capture a still image for an alarm | RTSP or HTTP snapshot URL: ________ |
| Printer | print a notice or label | IP ________ |
| Email, SMS | provider adapter with `send(to, subject, body, attachments)` | SMTP host or gateway: ________ |

Server interfaces: web and NCIP `http://<server>:8000`, SIP2 `<server>:6001` (`python run.py sip2 --host 0.0.0.0 --port 6001`).

## 5. Backup and restore

Backups use the SQLite online backup API, so they are consistent while the app runs.

| Task | Command |
|---|---|
| Manual backup | `python run.py backup --label before-change` (file in `data/backups/`) |
| Automatic backups | Before every real import and before every update |
| Restore | Stop the app, then `python run.py restore data\backups\<file>` |
| Verify | `python run.py health`, then open the dashboard and compare counts |

Recommended routine: nightly backup copied to separate media, and a restore test each quarter.

## 6. Update, upgrade, patch and rollback (offline)

1. Receive `rfid-library-<new>-offline.zip` and its hash. Verify the hash.
2. Unzip the new package anywhere. From the installation folder run `python update.py <unzipped new package folder>`.
3. The updater, in order: verifies every file against the manifest, installs the new wheels with no network, takes a backup, stages the new release, applies database migrations, runs a health check, and only then flips `current.json` to the new version.
4. If any step fails (bad hash, migration error, failed health check), the updater restores the backup and leaves the old release active. Output says `rolled_back: true`.

Manual rollback after a successful but unwanted update:

1. Stop the app.
2. Edit `current.json` back to the previous version (the old release folder is kept).
3. Restore the backup taken at the start of the update (`data/backups/*pre-<new version>*`) so the database schema matches the old code.
4. Start the app and run `health`.

Evidence: `docs/evidence/offline_lifecycle_report.json` shows install, update to 1.1.0, an automatic rollback of a deliberately broken 1.2.0, and rejection of a tampered package, all with the network unreachable.

## 6a. Data migration procedure

1. `python run.py import FILE --dry-run` and read the report. Nothing is written to the catalogue.
2. Review `data/reports/batch_<n>/error_log.csv` and fix the source, or accept the rejections.
3. `python run.py import FILE` (a restore point is taken first).
4. Check the reconciliation: source rows = loaded + rejected + duplicates. `balanced` must be true. The CLI exits with code 2 if not.
5. To undo: `python run.py rollback <batch_id>`. Refused if any item from the batch is on loan.

## 7. Troubleshooting runbook

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `health` says `schema_outdated` | Code newer than database | `run.py version` | Run `run.py init-db` or redo the update |
| Login always fails | Wrong user, inactive, or clock far off | Audit log `auth.fail` entries | Reset via admin, correct the clock |
| Everyone gets 401 suddenly | `jwt.key` replaced or secret changed | Is `RFIDLIB_JWT_SECRET` the same as before? | Users sign in again |
| Check-out refused with `MEMBER_BLOCKED` | Member blocked | `GET /members/<no>` | Unblock if appropriate |
| Check-out refused with fine message | Fine at limit | Member `fines_due` | Record payment |
| Check-out refused, device error | Security bit write failed | Reader connection | Fix the reader. The app fails closed by design |
| No gate alarm email | Provider down | `POST /admin/notifications/dispatch` shows `will_retry` or `failed` | Fix the provider. Queued mail is retried |
| Gate events missing | Event bus spooling | Dashboard gate counts, spool length | Restore the application layer. Events replay in order |
| Import rejects most rows | Wrong columns | `error_log.csv` reason column | Use the template headers: Accession No, Title, Author, ISBN, Publisher, Year, Location, Category, Barcode |
| Database errors after a crash | Interrupted write | `PRAGMA integrity_check` | Restore the last backup |
| Update refused: integrity | File changed in transit | Message names the file | Recopy the package |
| Port already in use | Another process | Netstat | Change `--port` |

Escalation notes for support: capture `run.py version`, `run.py health`, the last 50 audit rows (`GET /admin/audit?limit=50`) and the application log.

## 8. Security hardening checklist for production

- Remove or disable the demo accounts and demo members. Create real accounts with strong passwords.
- Set `RFIDLIB_JWT_SECRET` from a secret store or keep `jwt.key` readable only by the service account.
- Put a TLS reverse proxy in front. Restrict ports to the library LAN.
- Rotate the secret after staff leave (everyone signs in again).
- Keep backups on separate encrypted media.

## 9. Dependency licences (from `docs/sbom/SBOM.json`)

All 21 bundled components use permissive licences: MIT (most), BSD-3-Clause, Apache-2.0, PSF-2.0, MIT-CMU. These allow redistribution and offline use with the licence notices kept. Dependency audit with pip-audit found no known vulnerabilities on the pinned set at build time (`docs/evidence/pip_audit.txt`). Re-run it whenever the lock file changes.
