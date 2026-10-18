# Original website admission for queued article starts

A charged article outbox retains the target and verified source captured before
its original spend or first-article allocation. Retrying delivery must not replace
either value with today's default target or source.

Dispatch-key lookup still runs first. An already accepted child can be recovered
without new admission, another POST or another spend, even when original
verification history is incomplete. Existing run ownership and operation binding
remain responsible for admitting that recovered child.

Before a new website-article POST, the backend performs provider admission outside
SQL locks, then repeats stored admission under the existing organization →
connection → configuration lock order. Original connection, target and source
must be present; the same selected connection and target, original execution
attempt, source and current durable verification must still pass. A pending
schema3 proof blocks delivery even if the generation and source are unchanged.
New delivery also requires the original operation ID, attempt and deletion epoch;
legacy defaults cannot manufacture a typed execution identity.
That operation must still be pending/running for article generation with the
same original request key. Read access to a finished or unrelated operation
cannot authorize another child.
The transaction ends before the HTTP request; this check does not grant a worker
permission to ignore its own subsequent authority checks.

Legacy outboxes missing the original snapshot remain fail-closed for new work.
They are not enrolled, rewritten, recharged or retargeted. Existing bounded
delivery and dispatch-key reconciliation decide whether a worker already owns
the request. Unknown delivery retains the original financial obligation. Only
existing proven no-work settlement can release it; first-article reservations
remain owned by parent settlement and its complete child evidence.

Originally unbound, explicitly confirmed portable drafts retain their existing
admission path. A content-only flag cannot remove original website or operation
authority, and an unconfirmed value cannot bypass the website snapshot check.
Research delivery retains its separate organization/audience authority.

Database-free regressions exercise the actual charge producer, outbox consumer,
provider admission, schema3 promotion and parent allocation. Disposable
PostgreSQL CI additionally covers persisted configuration changes, bounded
unknown delivery, exact-key recovery and one-spend delivery. No database tests,
provider actions or financial repairs are run locally.
