# Contributing and change control

- Branches: `main` is always releasable. Work on `feature/<topic>` or `fix/<topic>`, open a merge request, one reviewer, CI green.
- Commits: small, meaningful messages that say why. Reference the requirement id (for example FR07, AC06).
- Change control: when behaviour changes, update the requirement, design note, code, test and documentation in the same merge request, and the row in `docs/requirements_traceability.csv`.
- Releases: tag `v<major>.<minor>.<patch>`. The final evaluation release is tagged `v1.0.0-eval`.
- Defects: record severity, reproduction steps, owner, status, fix version and retest evidence in `docs/05_TEST_PACKAGE.md` (or the issue tracker).
- Secrets and data: never commit passwords, tokens, keys, or real personal data. Demo credentials in `app/seed.py` are synthetic and for local use only.
