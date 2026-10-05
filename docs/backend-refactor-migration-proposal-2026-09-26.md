# Refactor migration integration proposal — 26 September 2026

Status: awaiting specific approval; no new migration file has been created or applied.

Create `content_factory/migrations/0042_merge_credentials_editorial.py` exactly as below. It joins the independent credential and editorial branches and has no schema or data operations. Existing migration files remain byte-for-byte unchanged.

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0040_backfill_github_credential_envelopes"),
        ("content_factory", "0041_writtenarticle_editorial_attribution"),
    ]

    operations = []
```

SHA-256: `84d023a10118f40b9d9971a8df1a6a2e06dfa96d148da8fb4a8e5b4d66603db9`.

Approve replay of the complete [356-file inventory](refactor-release-test-migrations-2026-09-26.json) only in newly created disposable local SQLite/PostgreSQL databases and GitHub CI test databases, using synthetic data and credentials. This includes the existing credential encryption/backfill migrations and the empty merge above. No existing local or production database will be used or migrated.

The inventory combines the previous 347-file Django 5.2.17 inventory, eight additional main migrations, and this one proposed merge. Existing repository migration hashes have been checked unchanged; package migrations will also be validated against the locked Django installation before replay. The test harness will reject any inventory mismatch.

This approval does not authorize a production release. The earlier note records production approval for credential migrations 0039/0040; release still requires a fresh exact production plan and explicit release authorization.

The repository's [AGENTS.md](../AGENTS.md) requires: “Never create, run, or apply a database migration without explicit user approval for that specific migration.” It also requires approval before database-backed Django tests.
