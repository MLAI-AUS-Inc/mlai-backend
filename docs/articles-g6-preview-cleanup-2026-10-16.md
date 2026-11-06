# Preview cleanup deadline

Lifecycle reconciliation records a `preview_cleanup_budget` receipt when it first
claims an operation with pending `stop_preview_run_ids`. The claim saves the
window before remote I/O. Queue age does not consume this window; crashes,
retries and partial progress do not restart it. Its deadline is 24 hours after
the first claim, bound to the original operation, connection and generation.

At the deadline, reconciliation records `preview_cleanup_abandoned`, moves the
operation to `needs_attention` and clears its next attempt. It makes no further
remote calls for this operation. All pending run IDs, cancellation references
and other cleanup scope remain in the payload for operational follow-up.
Abandonment is neither deletion proof nor a completed purge. It does not create
another owner action. Existing disconnect policies can stop earlier after five
passes without progress; this deadline also covers older lifecycle operations.

A missing budget starts on the next claim. A present but malformed, foreign or
inconsistent budget stops with `preview_cleanup_budget_invalid`; it cannot gain
a fresh window or produce an invented deadline incident. Confirmed cleanup
before the deadline closes the original window. Operations without preview
references keep their existing policy.

When `ARTICLES_OPERATIONAL_ALERTS_ENABLED` and the trusted goal cohort are
enabled, an accepted deadline transition reserves one incident in the existing
F3 outbox in the same transaction. A lost compare-and-set cannot create an
incident; a reservation failure rolls back the transition. Connection locking
precedes the operation update. Slack delivery happens outside database locks,
uses the configured operations channel and retains its stable message ID,
bounded retry and deletion fences. Flags remain off by default.

The incident contains the cleanup operation ID and causal window, with no
provider response, URL or run list. Its `goal_id` is null: a lifecycle operation
does not prove attribution to a site goal. Unknown company attribution remains
`unattributed`. It never manufactures zero usage.

The paired Content Factory changes validate Pages configuration and provide an
owner-run sweep for stranded previews. Adding droplet configuration, recreating
services, executing that sweep and confirming the old public URL returns 404
are separate owner operations. This backend change performs none of them and
does not claim the incident's production done-when is met.

Validation uses the database-free unit lane plus the existing disposable
PostgreSQL CI lane. PostgreSQL cases exercise repeated 202 responses until the
original deadline, durable claim-before-I/O, confirmed completion, lost leases,
transaction rollback and one acknowledged mock Slack delivery outside locks.
No migration or model change is required.
