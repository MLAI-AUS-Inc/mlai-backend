# Backend refactor release preparation — 3 October 2026

Status: integrated with Startup Pulse rewards and startup settings on 5 October 2026. The user specifically approved an empty graph join and disposable local/GitHub CI migration validation. Production migrations and credential backfills remain separately gated.

The integration preserves current startup profile drafts and AI answer evidence, scoped Valley MCP handoffs, current Slack timestamps/consent fixtures, code-only blue/green deployment and exact release freshness checks. It restores the useful local runtime/security refactor: complete worker inventory, Jobs queue consumer, scheduler failure propagation, tenant/financial ownership checks, shared JWT authentication and encrypted GitHub credential writes. New database-free test modules are assigned to CI; existing omission debt is unchanged.

## Approved disposable migration validation

The [read-only inventory](backend-refactor-migration-inventory-2026-10-05.json) records all 357 repository/package migration files from locked Python 3.11 / Django 5.2.17 and their SHA-256 hashes. The approved empty join produces one `content_factory` leaf.

The approved scope is:

1. Preserve and replay existing `content_factory.0039_encrypt_github_credentials`. It switches the two existing GitHub credential text fields to the compatibility encrypted field; table/column names and text/null storage remain unchanged. New writes encrypt; legacy plaintext and valid envelopes remain readable.
2. Preserve and replay existing `content_factory.0040_backfill_github_credential_envelopes`. It encrypts only plaintext in those two credential columns, validates existing ciphertext, retains null/empty values, processes locked batches of 100 and verifies completion. It is irreversible: it never restores plaintext. All writers must use the compatible new binary before a production backfill.
3. Create exactly `content_factory.0043_merge_credentials_website` with the source below. It joins existing `0040_backfill_github_credential_envelopes` and current main's `0042_website_connection_lifecycle` and contains no schema or data operations. It does not rewrite any existing migration.
4. Replay the resulting exact 357-file graph only in new disposable local SQLite/PostgreSQL and GitHub CI test databases using synthetic data/credentials. No existing local or production database is included. Hosted CI must pass the final tree before a merge/release.

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0040_backfill_github_credential_envelopes"),
        ("content_factory", "0042_website_connection_lifecycle"),
    ]

    operations = []
```

Approved 0043 SHA-256: `6da5a255baf16d6193415ba5e477ac71833eb3f0616e3dab5c2a87da8f8474ce`.

The user approved creating this empty join and replaying the documented graph only on disposable local/GitHub CI databases on 5 October 2026. This joins the credential branch to the later website-lifecycle branch without rewriting either. The former draft's skip-CI restriction is lifted by the integration commits. Production migration/backfill approval remains separate; the deployment script still requires the digest of an exact live migration plan before stopping writers or applying anything.

## Completed validation

- All 704 assigned storage-free contracts pass on locked Python 3.11 / Django 5.2.17 with network and database connections blocked. These cover runtime recovery, migration guards, shared auth, financial ownership, OAuth handoffs, Slack actions, reviewed startup profiles, article review and metric serialization.
- Dependency lock signature, CI module assignment, tracked-source credential scan, deployment shell syntax and diff whitespace checks passed.
- CI assigns 267 full modules, 10 partial modules and retains 126 recorded baseline omissions, with zero new omissions.
- Model drift checks pass. All 186 focused reward, registration, startup settings, profile, facade and website-connection tests pass on a fresh disposable SQLite database after replaying the approved graph (one PostgreSQL-only concurrency test skipped locally). Hosted CI validates PostgreSQL and the remaining regression lanes before merge.

The disposable replay harness now verifies the October 5 approved inventory before connecting and can seed current main at `0042_website_connection_lifecycle`. Public vector constraint validation preserves pgvector field validation while excluding its NumPy array only from scalar constraint expressions; no schema changes are required. Bootstrap responses reuse the already serialized runs, preserving the current performance regression contract.
