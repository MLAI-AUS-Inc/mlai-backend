# Proposed mentoring 0004: meeting capture policy and verification

Status: the user explicitly approved generation of this migration and application of the repository graph to a new disposable local PostgreSQL test database on 6 November 2026. Migration generated; local test application approved. Production application and deployment are not approved.

Dependency: mentoring.0003_instant_bookings. Proposed schema operations:

1. Add `MentorProfile.meeting_artifact_policy`: JSONField(default=dict, blank=True). Empty preserves existing mentor settings. Explicit automatic-booking activation defaults to version 1, transcription required, smart_notes required.
2. Add `MentorSession.meeting_provisioning`: JSONField(default=dict, blank=True). Stores canonical Meet space name, verified capture outcome/time, and policy version separately from immutable booking terms.

No data backfill, no changes to sessions, Roo balances, grants, or mentor activation. Reverse removes these two fields; do not reverse after activating capture-dependent bookings without a reviewed drain/recovery plan.

Requested test scope: generate only this migration; apply the repository migration graph to a new disposable local PostgreSQL database containing synthetic fixtures only; run mentoring booking/ledger/constraint tests. Production application/deployment is a separate scope. The exact graph inventory was recorded before test application in `mentoring-0004-disposable-inventory.json`.

Migration SHA-256: `ba97d36d9b13e323655e2bcf750bfb0c6bb4beab5af0e3d9c1657ebdbcedbe47`.

Approved disposable inventory: 385 migration files, recorded in the accompanying JSON. The test server uses a new local PostgreSQL cluster with synthetic provider responses; production credentials/data are excluded.
