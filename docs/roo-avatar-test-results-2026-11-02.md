# Roo avatar backend regression evidence — 2 November 2026

128 targeted tests passed on a fresh, disposable, socket-only PostgreSQL 17.11
cluster. This is source/local validation, not deployment or live-account proof.

The user explicitly approved `community_chat.0016_roo_avatar` and the
[inventoried 377 existing migrations](roo-avatar-migration-inventory.json) for
synthetic disposable local testing. The runner verified every existing migration
file hash before allowing the complete graph plus 0016 to be applied. Its
[proposal](roo-avatar-migration-proposal.md) records the exact schema and hash.

The existing credential-scrubbed cluster helper excluded `.env`, disabled
external networking and used local in-memory cache/email. It constructed a new
private Unix-socket cluster, created the test database, and removed both after
completion. No existing/shared database or production credentials were used.

Command (Python 3.11.14, Django 5.2.17):

```sh
python scripts/test_roo_avatar_disposable.py
```

The selection includes:

- Roo avatar pure catalog/eligibility tests, own-account GET/PATCH storage,
  public UUID scope, cross-device persistence, separate accounts/communities,
  strict input/locked-item rejection, optimistic conflicts, current-rank
  corrections, unknown history, cookie origins, session revocation/rotation,
  expired/inactive account rejection, purchased-wallet exclusion, and actual
  PostgreSQL concurrent first saves.
- Existing Volunteer policy, core services/API, presentation and concurrency
  regressions.
- Existing account profile, account session and session/device-binding suites.

Additional checks passed: standalone nine-test pure policy run; Python syntax;
Django system checks; migration dry-run (no changes); all documentation links;
CI test assignment (703 fully selected, one existing partial selection, zero
unassigned modules). The new pure/API suites are assigned to the normal CI
workflow, with explicit PostgreSQL concurrency coverage.

The raw run log is a local artifact at
`output/roo-avatar-progression/backend-tests.log`. New test counts and behavior
are reproducible through the checked-in runner; the log is not required to run
it. Frontend/device integration is validated separately by the MLAI Chat work.
