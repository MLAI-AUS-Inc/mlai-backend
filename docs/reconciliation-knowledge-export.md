# Reconciliation knowledge export

`GET /api/v1/integrations/reconciliation/knowledge-export` is the read-only
production knowledge contract for the standalone reconciliation agent. It uses
the same Roo service-key, Points Admin and organisation-domain gates as the
other reconciliation administration endpoints.

Required query parameters:

- `slack_user_id`: an active Points Admin
- `domain`: the organisation domain (defaults to the configured reconciliation
  domain)

The response is schema-versioned and contains per-collection counts and SHA-256
source hashes, plus a stable whole-snapshot `source_hash`. Observation times are
retained as `exported_at` and per-record `fetched_at`, but are excluded from
semantic hashing so an unchanged production dataset does not appear to conflict
on every pull.

Every record carries its backend source, type, ID, effective dates, verification
actor/time where applicable, stable version and fetch time. Collections include:

- reconciliation profile policy and source mappings;
- active verified party identities and reconciliation rules;
- configured Xero Event/Project tracking options;
- selected Luma events, Linear projects and active project members;
- sanitized confirmed outcomes, learning candidates and approved accounting
  tuples.

The export intentionally excludes connector credentials and refresh tokens, raw
provider payloads, raw messages and evidence, email addresses, notes, URLs,
bank-account identifiers/names, Xero write receipts and attachment content. The
agent must still encrypt the entire response at rest because verified party and
merchant relationships are sensitive operational context.

This endpoint has no write methods. Agent-side cache conflicts require an
explicit local admin decision and do not mutate this backend source of truth.
