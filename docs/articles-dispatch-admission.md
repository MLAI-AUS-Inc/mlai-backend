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

## Keyed first-article topic switches

For the admitted founder journey, `POST /api/v1/my-startup/first-article/switch-topic/`
with `Idempotency-Key` retains the authenticated owner, company, complete original
body, original run identity and prepared replacement before requesting a stop.
The key is a normal client UUID; both a custom topic and the catalogue shape
`{runId, topicId, customTopic: null}` use this contract. A changed body conflicts.

Replaying the same command recovers its exact replacement run or retained outbox,
including after a lost acknowledgement, process restart or completed execution.
The latest journey run is never a substitute acknowledgement. An active duplicate
returns `409 in_progress`; an unconfirmed original stop returns `503 switch_pending`.
Neither response admits another replacement or a paid fallback.

An owned original requires the existing terminal controller's exact closure,
fully drained journal and original grant release. Legacy originals require a
validated, bound no-effect cancellation receipt. Current owner consent and free
eligibility are rechecked before replacement admission, including in the actual
debit/outbox transaction. Existing no-key and non-admitted behavior is unchanged.
The command uses existing configuration receipts and requires no schema change.
The original execution binding is also checked by the cancellation transport
and under the terminal controller's claim lock. A concurrently granted execution
cannot be cancelled or closed by a saved switch for its predecessor.

`content_factory.tests_first_article_switch_commands` exercises this HTTP contract
against disposable PostgreSQL using synthetic provider responses, including
concurrent requests, unavailable cache, lost acknowledgements and owned closure.
# Keyed article controls

An `Idempotency-Key` on an owned article's approve, deny or single-run cancel
binds the full request body, action, current principal, company and original
Run/execution identity before prior approval consent can return a noop. Accepted
commands retain only private metadata in `founder_run_commands_v1`; replay
serializes the same Run with current permission checks instead of caching a
large response or submitting another Content Factory control.

Immediately before transport, the backend rechecks the active account, current
startup rights and exact outgoing admission/ticket. It persists `dispatching`
before HTTP. An unknown response never expires into permission to resend. A
known command acknowledgement does not establish execution status, stopped
effects, grant release or refund; those remain with the existing typed event
and settlement controllers. Changed bodies/actions/tickets conflict. Keyed group
deletion captures every original member and recorded PR URL before work, checks
each target again before transport and replays only the retained group outcome.
A new sibling cannot silently join an existing delete. Unkeyed and legacy
controls, resume and other actions retain their existing behavior.

New-journey keyed topic feedback/restore, written-article discard, and the
first-author settings save use private `founder_library_commands_v1` receipts.
Feedback changes and accepted receipts share one SQL transaction; replay reads
the original feedback row without reactivating a subsequently restored topic.
Discard captures the original article and lineage, retains uncertainty before
remote cancellation, and commits local deletion/keyword changes with its receipt.
A known remote refusal does not become execution or refund authority. Unknown
remote cancellation preserves the local article and cannot be resent by replay.
Current publication status and permission are rechecked before deletion.

Author saves retain the exact company/authors/default-author body before photo
storage and atomically commit the accepted receipt with existing settings writes.
Replays read current settings without rewriting old names or copying photos
again. An unknown storage result stays held. These receipts contain minimal
identity/outcome metadata, not complete cached bootstrap responses. Existing
receipt bindings remain enforceable when the cohort is disabled, so the same
header cannot escape through the legacy path with a changed body.

Once the library command namespace is enrolled, disabling the new-journey flag retains the keyed boundary: original accepted commands can project current authorized state, while unseen topic, restore, discard and author-only commands are refused before effects. Unrelated general settings and never-enrolled legacy callers keep their existing validation.
