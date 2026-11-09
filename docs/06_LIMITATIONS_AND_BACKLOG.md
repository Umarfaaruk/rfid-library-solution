# D9 Known limitations and production-completion backlog (NFR 09)

## Known limitations (stated plainly)

| # | Limitation | Consequence |
|---|---|---|
| L1 | All RFID hardware, the ILMS, email, SMS, print and CCTV are mocks | Real vendor behaviour, timing and failure modes are untested |
| L2 | NCIP and SIP2 are subsets built from the specifications, tested only against this project's own tests | No claim of conformance or certification. Needs testing against a real ILMS and real kiosk |
| L3 | Tag model is ISO 28560 style, not a full ISO 28560 encoder or decoder | Real tag programming needs vendor SDK and the library's data model |
| L4 | SQLite database | Fine for a single-site demo. Many concurrent writers need a server database |
| L5 | Acquisition, serials, spine-label printing, net cataloguing, virtual bookshelf not built | FR01 and FR02 are partial |
| L6 | Patron-card personalisation not built | FR04 partial |
| L7 | Roles are a fixed permission matrix with no editing screen | FR11 partial |
| L8 | No TLS in the app | Needs a reverse proxy. Passwords and tokens would otherwise cross the LAN in clear |
| L9 | No login throttling or lockout | Brute-force protection is backlog |
| L10 | Tokens are not revocable before expiry (60 min) | Deactivating a user blocks new logins only |
| L11 | Windows package builds, but was only run on Linux here | NFR03 needs a Windows Server 2022 run |
| L12 | Logs are plain Python logging | Structured JSON logs are backlog |
| L13 | Performance measured on one machine with synthetic data and no concurrency | See `docs/evidence/performance.json` |
| L14 | UAT scenarios were run by the author only | Real users have not tested it |
| L15 | The browser beep uses Web Audio and may be blocked until the first click | Real handheld beeper is the production cue |
| L16 | Single global circulation policy | No per-member-category or per-item-category rules |

## Production backlog (suggested order)

| Priority | Item | Why |
|---|---|---|
| P1 | Integrate with the real ILMS through the NCIP adapter in a test environment and read-only first | Core purpose |
| P1 | Real device drivers behind the existing Protocols (reader, handheld, gate, card reader) | Replace mocks |
| P1 | TLS, login throttling, token revocation, password policy, secrets store | Security |
| P1 | Server database option (PostgreSQL) with the migration runner or Alembic, plus load test | Scale |
| P2 | Windows Server 2022 service wrapper, install and update test on Windows 11 and Server 2022 | NFR03 |
| P2 | Structured JSON logs, metrics endpoint, alert on spool growth | Supportability |
| P2 | Role and permission editing, user admin screens, password reset | FR11 |
| P2 | Per-category circulation rules, holds and reservations, lost items | Library practice |
| P3 | Serials, acquisitions, spine-label and patron-card printing | FR01, FR04 |
| P3 | Net cataloguing and virtual bookshelf | FR02 |
| P3 | Signed update packages (not just hashes), reproducible builds | Supply chain |
| P3 | Accessibility audit of the console, localisation | Quality |
| P3 | Penetration test and formal conformance tests | Certification claims |

## Risks

| Risk | Mitigation in place | Remaining |
|---|---|---|
| Vendor SDK differs from the mock contract | Protocols isolate the change | Contract may need to grow |
| Bad migration corrupts data | Dry-run, staging, reconciliation, automatic backup, rollback by batch | Rehearse on a copy of real data |
| Gate alarms during ILMS outage | Gate decides locally | Needs sync of loan state to the gate controller |
| Update interrupted by power loss | Pointer flips last, backup taken first | Add a recovery command |
