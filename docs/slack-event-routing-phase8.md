# First-event routing for private Slack conversations

This describes source behavior, not a completed production rollout. Phase 8 uses
the existing `MESSAGE_SYNC_EVENT_ROUTING` switch, default `false`, and requires
`MESSAGE_SYNC_ENABLED=true`. It introduces no migration, new provider scope or
new consent. Production deployment and enablement remain owner steps under the
[migration runbook](slack-member-phase3-operations.md).

## Routing and authority

An authorized unknown group-DM or private-channel callback is parked as
owner-scoped ciphertext in the existing connection checkpoint. When the switch
is enabled, that same transaction coalesces an ID-only `(grant, channel)` wake
with a generation and scheduling metadata. Ciphertext remains the only queued
copy of the body. Repeated callbacks retain the original parked timestamp and
cannot reset an owner or source cooldown. Missing wake metadata can be recovered
from the durable parked events after restart.

The event selector can claim due work immediately after an owner's recent
directory sweep. It takes the same user → grants → connection discovery lease;
it cannot race the owner's ordinary discovery or overwrite another worker's
claim. Three turns prefer events and the fourth retains ordinary opens/sweep
work. Bounded discovery slots prevent one slow owner from occupying every slot.
Workspace/owner rotation, shared app/method budgets and Slack `Retry-After`
remain authoritative.

Routing uses the existing `staged_routing._route_channel` implementation. A wake
does not grant access: source scopes, current consent and OAuth generation,
positive membership, Slack Connect exclusion, archive/closed-source rules,
verified-device audience and registration checks still apply. Every provider
call and stored response rechecks the current authority and lease. An expired
worker or revoked grant cannot use its old wake to create or refill a room.
Uncertain work remains parked, with the ordinary sweep as backstop. A newer
event generation arriving during settlement retains its own wake.

The event switch does not require `MESSAGE_SYNC_TARGETED_STAGED_ROUTING`.
Disabling the event switch stops new event claims and event-lane provider calls;
the established targeted/sweep path continues to own retained ciphertext.
Normal revocation and privacy cleanup remain active regardless of either switch.

## Delivery evidence

`slack_staged_routed path=event latency_ms=…` measures parked → delivery queue.
It is useful routing evidence but does not prove delivery. The private delivery
row separately retains a server-owned original parked time and path. Both single
and batch adapter acknowledgements record parked → confirmed latency once in
that row, including queue waits, failed attempts and adapter delay. Outside-window
or superseded no-ops and undeliverable tombstones are excluded from confirmed
samples. Callback metadata cannot supply the internal timing marker.

After the completion transaction commits, `slack_staged_delivered` emits only
path, count and milliseconds. Aggregate latency telemetry is best effort; a
missing log or process-local counter is not proof of a missing or fast delivery.
The durable timing marker is the completion evidence.

`python manage.py message_sync_status` reports event routing backlog and outcomes,
including parked work, deferrals, expired leases and terminal outcomes. Its
delivery section distinguishes event, targeted and discovery paths, recent
confirmed samples and outstanding work. Samples are bounded and report
truncation explicitly. `under_15s` is strictly less than 15,000 ms;
`at_least_15s` includes the exact 15-second boundary. These counts are threshold
evidence, not an invented exact percentile. Inspect outstanding work, oldest
age, failures, sample window and truncation before drawing a latency conclusion.
Do not discard delayed or failed canary attempts to improve a reported p95.

## Validation and canary

The workflow selects the new event tests in SQLite and PostgreSQL, with only
database-free diagnostics in the no-database lane. Database-backed tests require
the specifically approved migration inventory, even though this phase adds no
migration. Tests cover atomic staging/wake rollback, duplicate coalescing,
restart recovery, cooldowns, concurrent event/sweep claims, replaced leases,
new arrivals during settlement, flag independence, private routing fences and
single/batch acknowledgement timing. PostgreSQL tests exercise actual row locks;
SQLite does not prove concurrency behavior.

Before enablement, release the reviewed source with the event switch off and
verify normal bridge health and unchanged private/public behavior. Then use only
the owner's approved staff test scope and an actual group-DM or private-channel
first event. The public Members-channel canary alone cannot prove this phase.
Record all attempted events and their original parked/confirmed timings, quota
deferrals and outstanding outcomes, without message bodies or member keys.

The acceptance target is actual canary p95 parked → confirmed delivery **under
15 seconds**. Choose and record the percentile method and sample cohort before
measurement; retain the full denominator. Do not claim acceptance with an empty
sample, unmeasured/stuck attempts or truncated evidence. Respect consent,
membership and provider limits even if cold provisioning exceeds the target.
Record the measured result before marking Phase 8 complete.

Rollback is to disable `MESSAGE_SYNC_EVENT_ROUTING` through the approved
deployment configuration, leaving ciphertext, ordinary discovery and cleanup
intact. There is no schema downgrade. A code rollback still requires the normal
release process; never delete parked private data or reset consent/leases as a
latency shortcut.
