# Multiple source accounts: Gmail schema approval proposal

Status: explicitly approved by the owner on 2026-11-03: "Approve this migration, tests and deployment" (reply to call_17c6476bbee84e41aca7b310c13958a2). Created as integrations/0051_google_connection_accounts; not yet applied to production. Only the exact three operations below are authorized.

The owner requested multiple accounts per integration inside MLAI and combined
CRM sources. This is separate from Codex's plugin connections.

## Exact proposed migration

Change only `integrations.GoogleConnection`:

1. Add `provider_account_id`: nullable, blank-allowed `CharField(max_length=255)`.
   Store the Google-authenticated immutable `sub`, not the editable email address.
   Existing rows stay NULL; no network calls or data backfill in the migration.
2. Remove `uniq_google_connection_user_org`, which currently permits only one
   mailbox for a user/startup pair.
3. Add `uniq_google_user_org_account` over `(user, organization,
   provider_account_id)`, using `nulls_distinct=False` on PostgreSQL. This permits
   distinct verified Google accounts but prevents duplicate identities. One
   legacy NULL-identity row per user/startup remains compatible.

No token field, permission, contact, consent, unsubscribe, hold, sender, roster,
cap or existing account row is changed by this migration. Existing foreign keys
retain the same connection IDs. Other providers already have account identity
columns; this proposal does not authorize other schema changes.

## Application behavior required before activation

- On OAuth return, verify the Google subject before selecting a connection.
  Reconnect only that identity; never borrow a refresh token from another account.
- Attach a verified subject to an existing legacy row only when the returned
  verified email identifies that exact row unambiguously. Otherwise preserve it
  and create a distinct verified connection.
- Show each account, label, sync status and per-account disconnect/reconnect.
- Let the owner choose one account per provider for reports, pin those choices
  to new runs, and preserve all existing run pins. The owner explicitly chose
  "Combine CRM data; choose accounts for reports" (call_bb015c8c733d4fe8a825bca7cc7f7c8f).
- Keep user/startup authorization boundaries. CRM ingestion requires its own
  explicit admin-selected source set; connecting a founder's private account
  never adds it to the community CRM.
- Do not combine financial balances or metrics merely by summing accounts.
  Deduplicate source records by verified provider identity and source object ID;
  retain per-account provenance and report missing data as unavailable.
- Luma API keys identify calendars, not all hosted events for a person. Discover
  calendar identity via `/v1/calendars/get`; cover host-only events through a
  separately verified supported host source. Do not claim complete host coverage
  from calendar keys alone.

## Approval requested

Approval to create this one migration, replay the unchanged existing 378-file
approved schema plus this migration in fresh disposable local/CI databases,
and apply this exact schema change through the normal reviewed deployment after
all required checks pass. Any other schema change needs separate approval.

## Verification and rollback

Test two accounts under one startup, reconnect identity, token isolation,
legacy-row adoption, two startups, per-account disconnect, source-set pinning,
partial failures and duplicate source objects. Verify existing protected CRM
preferences and send state before and after deployment.

Rollback application behavior by disabling new multi-account selection while
retaining all existing rows and tokens. Do not re-add the old one-account
constraint or delete accounts to force a downgrade after multiple accounts have
been added. A schema rollback would need its own reviewed data plan.
