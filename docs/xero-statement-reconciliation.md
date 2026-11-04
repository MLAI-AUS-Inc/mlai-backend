# Xero statement reconciliation

The standalone reconciliation agent observes Xero's browser-only bank-feed
queue, submits evidence-backed suggestions, and asks this backend to create an
authorised Spend Money, Receive Money, or exact bill payment. Xero's Accounting
API does not expose the unreconciled statement queue or the final Match/OK
action, so a human still performs the final match in Xero.

## All-account capture contract

`GET /api/v1/integrations/reconciliation/bank-accounts` reads Xero's Accounts
API and returns the exact active `BANK` account catalogue for the connected
tenant. The endpoint is read-only and uses the same Roo API-key, Points Admin,
and organisation gates as other reconciliation administration routes.

Successful catalogue responses are reused for at most 60 seconds, scoped to the
organisation, connection and tenant. This avoids one Accounts API call per
account during a coherent capture while bounding account-change freshness.
Expired or failed responses are never used to authorize a capture or posting.

Accounting API HTTP 429 responses establish a shared app/tenant cooldown using
Xero's `Retry-After` interval. Subsequent bounded HTTP requests return a 429
locally until that deadline instead of repeatedly calling Xero. Capture errors
report the remaining wait explicitly and continue to require a verified account
catalogue. A cooldown does not authorize stale data, offline fallback, or a
change to Xero posting gates. No credential is retained in the cooldown cache.

An official Xero Uncoded Statement Lines CSV is imported as one schema-version
2 capture with one scan per active account, including accounts with zero rows.
Every scan binds the tenant, account position, exact active-account ID list,
report range, source file hash, and explicit whole-organisation coverage
confirmation. A partially delivered capture remains in storage but blocks
readiness and cannot start an agent run. A complete group contributes active
candidates from every account to one reconciliation run.

The CSV does not expose Xero's stable DOM statement-line identifier. Synthetic
IDs therefore include duplicate cardinality. Identical duplicate lines are
never executable: the writer cannot safely distinguish which physical bank-feed
line survived a later export.

## Posting gates

The posting preview uses the statement line's own bank-account ID. For a
schema-version 2 capture, it refreshes Xero's active bank-account catalogue and
rejects a line whose account is no longer active. An aggregate agent-run preview
refreshes that catalogue once and reuses the verified result for every line in
the run. The preview retains the
existing source-hash, confidence-axis, accounting, tax, duplicate, document,
and Event-or-Project tracking gates. Execution creates the authorised Xero item
that appears as a green candidate; it never presses the final Match/OK action.

Legacy single-account observations and the configured payout bank account
remain backward-compatible. New whole-organisation runs should use the live
catalogue and schema-version 2 CSV capture path.

This change does not add or alter database models and requires no migration.
