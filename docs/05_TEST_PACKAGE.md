# D6 Test package

## 1. Test plan

| Level | What | How | Where |
|---|---|---|---|
| Static | Style, imports, common bugs, security lint | `ruff check` (rules E, F, I, B, UP, S) and `ruff format --check` | CI and local |
| Unit and functional | Services and rules | pytest, one file per acceptance scenario | `tests/test_ac*.py` |
| Integration | API, NCIP over HTTP, SIP2 over TCP, CLI, UI assets | FastAPI test client, real socket, real CLI | `test_ac03`, `test_cli`, `test_ui` |
| Migration | 20,000 row import matches a ground-truth file with planted defects | Generator writes data and a truth file | `test_ac01_migration.py` |
| Regression | Whole suite on every change | CI | `.github/workflows/ci.yml` |
| Performance | Import rate, search latency, circulation latency | `scripts/measure_performance.py` | `docs/evidence/performance.json` |
| Security | Auth, roles, token forgery, XML entities, injection, dependency audit | pytest plus pip-audit | `test_ac07`, `test_ac03`, `test_ac09`, `pip_audit.txt` |
| Backup, restore, offline update, rollback | Real install with the network unreachable | `scripts/verify_offline_lifecycle.py` | `docs/evidence/offline_lifecycle_report.json` |
| UAT | Scripted user journeys in a browser | Manual scenarios below, plus a browser run captured in screenshots | `docs/evidence/screenshots/` |

Entry criteria: lint clean. Exit criteria: all tests pass, no open critical or high defects, every FR, NFR and AC mapped in `requirements_traceability.csv`.

## 2. Results (latest run)

| Check | Result |
|---|---|
| Automated tests | 97 passed, 0 failed, 95 percent line coverage (`docs/evidence/pytest_coverage.txt` lists every test) |
| Lint and format | clean |
| Dependency audit | No known vulnerabilities in the 21 pinned components (pip-audit) |
| Offline lifecycle | All steps pass: install with no network, health, import 20,000 rows, API health, search, update to 1.1.0, automatic rollback of broken 1.2.0, DB fingerprints identical after rollback, tampered package rejected |

Tests per acceptance scenario: AC01 9, AC02 8, AC03 14, AC04 12, AC05 3, AC06 12, AC07 10, AC08 7, AC09 8, AC10 4, CLI 7, UI 3.

## 3. Performance (single machine, no concurrency, synthetic data)

Source: `docs/evidence/performance.json`, 20,100 source rows.

| Measure | Result |
|---|---|
| Import (validate, dedupe, load, reconcile) | 0.96 s, about 20,900 rows per second, 19,400 loaded |
| Search, p50 / p95 / max (200 queries) | 3.9 ms / 14.0 ms / 18.8 ms |
| Check-out, p50 / p95 (300) | 1.9 ms / 2.8 ms |
| Check-in, p50 / p95 (300) | 1.6 ms / 2.1 ms |

These are in-process service timings, not network or browser timings, and not a load test.

## 4. Manual UAT scenarios

Run in a browser against a seeded demo database. Record pass or fail and initials.

| ID | Scenario | Steps | Expected |
|---|---|---|---|
| M1 | Public search | Open `/`, search "Geology" | Titles with availability labels, no sign-in |
| M2 | Bad login | Wrong password | Message shown, audit shows `auth.fail` |
| M3 | Card login | Card `CARD-LIB` | Signed in as librarian |
| M4 | Lend | Member M0001, item from the catalogue | Due date shown, status on loan |
| M5 | Reference refusal | Lend a Reference item | Refused with reason |
| M6 | Block | Block M0002 then lend | Refused |
| M7 | Tag | Tag item, see tag UID and secured status | Link shown |
| M8 | Inventory | Scan some tags of a shelf, skip one, add a foreign one | Missing and misplaced listed, low beep |
| M9 | Gate alarm | Walk a tagged, not-lent item through | Alarm, image saved, email queued |
| M10 | Gate pass | Lend an item then walk it through | Pass, no alarm |
| M11 | Dashboard | Filter by location | Numbers change |
| M12 | Assistant limits | Sign in as assistant, open RFID tools | Actions refused |
| M13 | Backup and restore | Back up, import more rows, restore | Counts match the first state |
| M14 | Offline update | Run the lifecycle script | All steps pass |

## 5. Defect log

Defects found and fixed while building. Severity: H high, M medium, L low.

| ID | Sev | Found by | Description | Fix | Version fixed | Retest |
|---|---|---|---|---|---|---|
| D01 | H | Offline lifecycle script | Updater integrity check failed because Python wrote `__pycache__` into the new package | Updater now runs `python -B` | 1.0.0 | `test_ac09_update.py` and lifecycle script pass |
| D02 | H | AC09 test | A migration that failed after partial DDL left a half-created table, because SQLite can commit DDL early | Backup restore is the safety net, covered by a test with a deliberately broken migration | 1.0.0 | `test_failed_migration_with_partial_ddl_is_undone_by_restore` |
| D03 | M | AC03 tests | NCIP member lookup did not return the expected result | Lookup corrected | 1.0.0 | `test_ncip_lookup_user_and_item` |
| D04 | M | AC03 tests | A SIP2 reply was parsed from the wrong character offset | Offset corrected | 1.0.0 | SIP2 tests in `test_ac03_protocols.py` |
| D05 | M | Security review | Signing key was short and tests used a weak value | Per-install generated secret (`load_or_create_secret`), longer test keys | 1.0.0 | `test_ac07_roles.py` |
| D06 | M | Data test | Generator and tests disagreed on the count (19,999 vs 20,000) | Generator reports truth, tests compare to truth | 1.0.0 | `test_ac01_migration.py` |
| D07 | L | Build | Partial unique index declared with a raw string instead of `text()` | Fixed | 1.0.0 | Whole suite |
| D08 | M | Review | Real imports did not take a backup first (NFR 01) | Automatic pre-import backup added | 1.0.0 | `test_real_import_takes_a_restore_point_first_but_dry_run_does_not` |
| D09 | H | Review against SOP | No web UI existed, only an API (FR02) | Added console and public OPAC | 1.0.0 | `test_ui.py` and browser run |
| D10 | L | Review | SIP2 listener not runnable from the command line | Added `sip2` command | 1.0.0 | `test_sip2_command_listens_and_answers_login` |

Open defects: none known at this time. Limitations are listed in `06_LIMITATIONS_AND_BACKLOG.md`, not counted as defects.

## 6. Security checks performed

| Check | Evidence |
|---|---|
| Forged, expired, `alg=none` and tampered tokens rejected | `test_tampered_and_expired_tokens_rejected` |
| Role matrix across 8 endpoints and 3 roles | `test_permission_matrix` |
| Same answer for wrong user and wrong password | `test_wrong_password_rejected_without_hint` |
| Card UID masked in the audit log | `test_unknown_card_rejected_and_audited_with_masked_uid` |
| XML entity and DTD attack on NCIP | `test_ncip_rejects_dtd_and_entities` |
| SQL injection through search | `test_search_index_is_maintained_by_triggers`, `test_opac_search_is_public_and_read_only` |
| Public OPAC exposes no private data | `test_opac_search_is_public_and_read_only`, `test_opac_is_not_a_back_door_to_staff_endpoints` |
| Static file route cannot read other files | `test_console_and_assets_are_served` |
| Tampered update package rejected | `test_tampered_package_is_rejected_before_anything_changes` |
| Vulnerable dependencies | pip-audit clean |

Not done: penetration test, fuzzing, load test, TLS, lockout.
