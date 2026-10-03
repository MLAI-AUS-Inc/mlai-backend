# Backend refactor release preparation — 3 October 2026

Status: draft source PR prepared against current main; specific migration creation/replay approval and hosted validation remain pending. No migration was created or executed in this preparation.

The integration preserves current startup profile drafts and AI answer evidence, scoped Valley MCP handoffs, current Slack timestamps/consent fixtures, code-only blue/green deployment and exact release freshness checks. It restores the useful local runtime/security refactor: complete worker inventory, Jobs queue consumer, scheduler failure propagation, tenant/financial ownership checks, shared JWT authentication and encrypted GitHub credential writes. New database-free test modules are assigned to CI; existing omission debt is unchanged.

## Specific migration approval proposal

The [read-only inventory](backend-refactor-migration-inventory-2026-10-03.json) records the 355 existing repository/package migration files from locked Python 3.11 / Django 5.2.17 and their SHA-256 hashes. Two independent `content_factory` leaves remain, so the graph cannot pass database-backed validation yet.

The exact requested scope is:

1. Preserve and replay existing `content_factory.0039_encrypt_github_credentials`. It switches the two existing GitHub credential text fields to the compatibility encrypted field; table/column names and text/null storage remain unchanged. New writes encrypt; legacy plaintext and valid envelopes remain readable.
2. Preserve and replay existing `content_factory.0040_backfill_github_credential_envelopes`. It encrypts only plaintext in those two credential columns, validates existing ciphertext, retains null/empty values, processes locked batches of 100 and verifies completion. It is irreversible: it never restores plaintext. All writers must use the compatible new binary before a production backfill.
3. Create exactly `content_factory.0042_merge_credentials_editorial` with the source below. It joins existing `0040_backfill_github_credential_envelopes` and current main's `0041_writtenarticle_editorial_attribution` and contains no schema or data operations. It does not rewrite any existing migration.
4. Replay the resulting exact 356-file graph only in new disposable local SQLite/PostgreSQL and GitHub CI test databases using synthetic data/credentials. No existing local or production database is included. Hosted CI must pass the final tree before a merge/release.

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0040_backfill_github_credential_envelopes"),
        ("content_factory", "0041_writtenarticle_editorial_attribution"),
    ]

    operations = []
```

Proposed 0042 SHA-256: `84d023a10118f40b9d9971a8df1a6a2e06dfa96d148da8fb4a8e5b4d66603db9`.

The migration file is absent. The draft's `[skip ci]` commit prevents the normal pull-request workflow from indirectly replaying unapproved migrations. After specific approval, create the exact graph join, regenerate the approval-bound inventory and run the disposable fresh/main-upgrade, credential row-lock and broader application lanes. Before production deployment, obtain a fresh exact live migration plan and its separate specific approval; this proposal does not authorize a production backfill.

## Completed validation

- 133 contracts passed on Python 3.11.14 / locked Django 5.2.17 with network and database connections blocked; no Django test runner or test database was started. They cover runtime recovery, mocked migration inventory/approval guards, shared auth, financial ownership, OAuth handoffs, Slack actions, article review and metric serialization.
- Dependency lock signature, CI module assignment, tracked-source credential scan, deployment shell syntax and diff whitespace checks passed.
- CI assigns 242 full modules, 11 partial modules and retains 126 recorded baseline omissions, with zero new omissions.
- Migration graph inspection was read-only. Full model/database validation remains blocked on the proposed empty graph join and specific replay approval.
