# Slack member migration: Phase 4a operations

Phase 4a implements public-channel intake, membership observations, Members
room metadata and audience synchronization. This document describes the source
and release gates. It records no Phase 4a production deployment or live Slack
canary result. Phase 4b member-token history and Phase 4c member posting, recorded
poster identity and echo suppression require later PRs before activation.

The existing [Phase 3 foundation](slack-member-phase3-operations.md),
[bridge contract](mlai-chat-bridge-contract.md), and
[Phase 8 private first-event runbook](slack-event-routing-phase8.md) remain
separate contracts. Public-channel canary results cannot establish private or
MPIM first-event latency.

## Initial release configuration

Deploy this source with these migration values unchanged:

| Setting | Initial value |
| --- | --- |
| `SLACK_MEMBER_CHANNELS_ENABLED` | `false` |
| `SLACK_COMMUNITY_SEND_AS_MEMBER` | `false` |
| `SLACK_COMMUNITY_SOURCE` | `bot` |
| `MESSAGE_SYNC_EVENT_ROUTING` | `false` |

New member-channel activity additionally requires `MESSAGE_SYNC_ENABLED` and
`SLACK_OWNER_INVENTORY_ENABLED`. Preserve their separately approved deployment
values; their dependency does not authorize changing them. Validate release
configuration with `scripts/validate_message_sync_deploy_config.py` in the
normal release preflight. Phase 4a adds no schema or consent version.

`SLACK_MEMBER_CHANNEL_ALLOWED_IDS` is an optional CSV scope, independent of the
four migration values. The configured workspace must also match
`MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID`.

- An exactly empty value permits every otherwise eligible channel **when
  member channels are enabled**. Empty does not mean disabled.
- An explicit list permits only its exact channel IDs. Spaces/tabs around each
  ID are accepted; line breaks, NUL, empty entries, whitespace-only input, invalid IDs, more than
  1,000 entries or more than 25,000 characters are rejected by deployment
  validation and denied by the runtime scope check.
- IDs must match `C[A-Z0-9]{2,23}`. This checks scope and syntax; a `C` prefix
  does not establish public privacy.
- Before the first separately approved enablement, constrain this value to
  the reserved public canary `C0CAPNW3QLT` (`#mlai-sync-canary`). Widening the
  list or replacing it with empty is a rollout change requiring owner approval.

Turning the member flag off stops new public member intake, membership
additions, member-channel metadata refresh probes and provisioning. Existing
owner-inventory and private-chat baseline behavior remains separately gated.
Existing-room authority reductions
continue: rollback must still remove keys that have lost authority and archive
already mapped sources that became private, shared, deleted or archived.

## Consent, membership and client capability

The owner chose existing Slack consent. Accepted v3/v4/v5 consent, an active
account, exact current OAuth/workspace identity and `channels:read`,
`channels:history`, `chat:write` remain required for public readers. Reader and
room-membership eligibility requires neither AI consent nor an attribution
identity link. Posting policy remains a later Phase 4c/5 concern.

Membership is observed for the exact Slack subject, never inferred from a
callback recipient, `item_user`, cached channel prefix or an info reader chosen
only to inspect metadata. A newer observation wins; an equal-time leave wins
over a join. Only a complete, exact-authority public inventory sweep can prove
departures by absence. Unknown, malformed, shared or partial observations do
not establish a complete sweep. Public inventory uses 200-item pages and at
most three pages per enabled turn, persisting unfinished work for follow-up.

Pause, consent renewal/change/withdrawal and OAuth authority changes advance a
membership proof floor in existing connection state. Resume/reconnect retains
the existing consent choice but cannot restore an older joined observation.
Fresh authoritative membership evidence at or after the floor is required.
Malformed proof floors deny entitlement. Existing-row leave reductions remain
available even when an old observation cannot authorize a join.

Clients opt into the new catalog type by sending:

```http
X-MLAI-Slack-Catalog-Kinds: im,mpim,private_channel,public_channel
```

The Slack status, owner-inventory and open endpoints accept this header. Missing
or malformed capability never exposes a Members room ID as a new chat type.
Existing Community mappings retain their legacy presentation. A Members room
is ready only when the requester has a current joined grant and the requesting
key is currently verified and unrevoked for the active account. Members rooms
include all such devices; the separately limited private-room helper is unchanged.
Every new device must wait for a full acknowledged audience containing it.

Readiness also requires a public source observation no older than five minutes,
no hold or pending room/audience synchronization, and an exact adapter
acknowledgement matching source, destination, generation and the complete
current audience hash. A current member opening a room that is still being
prepared queues a bounded refresh and receives HTTP 202 with
`state=provisioning`, `stage=provisioning`, and no room ID.

## Callback and source privacy

Signature verification remains at the callback view. Inline and durable intake
share this order: member router, existing private intake, then bot public
fallback. An unmatched configured user-app callback is handled without reaching
the generic public store. Durable callbacks prelock their bounded account
cohort in sorted order before hints or channel locks; a newly discovered owner
forces an atomic retry rather than a lower-order account lock or token refresh.
Inbox completion and the resulting outbox writes remain atomic.

Public member routing requires the configured user app and workspace plus
positive public source proof. Known private registrations, including retired
ones, and positive private/Slack Connect metadata override public hints or
cached mapping state. Ordinary messages, edits, deletes, reactions, membership
and lifecycle events use their own envelope shapes:

- Join/leave `channel_type=C` and a `C` channel ID are insufficient public
  proof. Membership updates use the signed event's exact `user` subject.
- Lifecycle create/rename can carry a channel object; reaction sources are in
  `item.channel`. Missing message-style type fields do not select public intake.
- Members activity and Community mutations, reactions and join controls need
  fresh authoritative public metadata. A legacy Community ordinary create can
  use its explicit signed `channel_type=channel` contract when current info is
  unavailable. Unknown/stale sources queue metadata refresh without content or
  membership additions.
- Known private, shared, deleted and archived sources close locally even with
  expansion off. An unknown leave can queue metadata refresh only while
  enabled and in scope; it cannot create an off-flag membership tombstone.
- All known callback ID changes, including unmapped rows, conservatively hold
  with `rekey_requires_room_migration`. The current documented Slack transition
  can represent a private/Connect ID change and supplies no new public proof.
  The retained unmapped `rekey_channel` foundation is not activated by this
  router. Enabling it needs new-ID privacy checks, source-proof reset and an
  old-ID tombstone/lease fence against in-flight refresh recreation; no public
  or membership authority is carried to the new ID in this release.

Public receipts use bounded source identities shared by bot/member copies:
message timestamp, operation and revision; or reaction target, name, actor,
add/remove and event timestamp. Later edits and reaction re-adds remain distinct;
thread broadcast promotion is retained. When the gate is off, or complete valid
source identity is unavailable, legacy event-ID receipt behavior is retained.
Receipt keys contain no message body. This prepares later dual-source intake;
it is not evidence that dual-source observation has begun.

## Audience synchronization and failure handling

New rooms start empty and disabled. Existing Community room IDs and their
applied audience remain immutable. Members audience synchronization derives the
union of all current verified, unrevoked device keys for each active consenting
joined account. There is no per-account device cap. At most 1,001 distinct
canonical keys are read to prove the complete room-wide 1,000-key boundary.
Overflow returns no usable set, holds the room as `audience_too_large`, empties
the remote audience through the normal CAS lane and denies every content/read
gate. It never truncates a desired audience or hashes overflow as an empty
valid audience. Its canonical hash matches the relay protocol.

Each execution UUID is committed before its first adapter attempt. The worker
then locks current Users, grants, connections, devices and memberships before
Channel, re-derives authority, inspects the actual relay audience and sends an
exact generation/set compare-and-swap. A conflict yields to a new attempt and
fresh authority selection; an obsolete command is never silently rebased.
Only a matching adapter response/inspection records `audience_applied` and can
open the local delivery gate. No complete participant-key list is stored in that
acknowledgement.

Members intake rechecks the current local acknowledgement under the Channel
lock. While an audience is pending, the same encrypted inbox receipt remains
pending with a bounded five-second scheduling delay; membership observations
and wakes commit without treating its content as delivered. Edits, deletes and
reactions remain recoverable through that receipt too. Previously waiting
receipts survive flag rollback or stale public metadata. A newer message from
an already-current joined member updates membership ordering without another
audience wake; fresh proof after an old epoch or expired probe still wakes it.

Immediate local reduction closes new intake and claims. It does not fence a
delivery claimed before the reduction. Before any Members activation, Phases
4b/4c must add and test a current source/audience/provider-send fence that spans
bounded I/O, including retries of frozen envelopes. A claim, local pending flag
or earlier acknowledgement alone does not establish that guarantee.

Authority hooks persist pending work in the local denial transaction. Captured
reductions run after commit and never add keys; they do not hold a revoked user's
transaction while acquiring other Users. A bounded immediate attempt covers at
most five rooms; the maintenance worker continues remaining pending work and
existing-room audits independently of expansion flags. Bulk/admin changes are
also caught by these audits. Closed Community destinations have a separate
existing-room archive lane that preserves their audience and supplies no new
room metadata.

Maintenance runs in bounded turns: pending audience work reserves an audit slot;
attempt timestamps rotate failed work, and metadata refresh leases cannot erase
a newer queued observation. This bounds work per turn, not completion latency.
Provider budgets, no reader, repeated conflicts or an unavailable adapter can
leave pending reductions. Local denial remains committed; it does not prove
remote key removal. Inspect retained pending/error state and actual adapter
authority after recovery before declaring completion. Do not clear pending,
restore a stale desired key set or widen scope to make a check pass.

Holds such as `name_conflict`, `audience_too_large`,
`audience_authority_unavailable`, `immutable_room_audience` and
`rekey_requires_room_migration` require investigation. Reader absence remains
`no_reader`; selecting an info reader is not a grant of channel membership.
Keep diagnostics to approved IDs, aggregate counts, bounded error codes and
acknowledgement outcomes. Do not dump credentials, callback bodies, connection
cursors or full participant lists.

## Validation and later owner gates

Run the database-free contracts with the repository guard:

```sh
python scripts/test_without_database.py \
  integrations.tests_member_channel_events_phase4a \
  integrations.tests_member_public_receipts_phase4a \
  integrations.tests_member_channel_catalog_phase4a \
  integrations.tests_member_channel_inventory_api_phase4a \
  integrations.tests_member_callback_authority_phase4a \
  integrations.tests_slack_member_membership_epoch_phase4a.MemberMembershipFloorTests
```

After specific approval of the disposable migration inventory, the canonical
SQLite/PostgreSQL jobs in `.github/workflows/deploy.yml` must also run
`integrations.tests_slack_channel_membership_phase4a`,
`integrations.tests_slack_owner_inventory_phase4a`,
`integrations.tests_member_channel_maintenance_phase4a`,
`integrations.tests_community_bridge_member_audiences_phase4a`,
`integrations.tests_slack_member_membership_epoch_phase4a`,
`integrations.tests_member_public_receipts_database_phase4a` and
`integrations.tests_member_callback_authority_concurrency_phase4a`, plus
`integrations.tests_member_pending_intake_phase4a`, together
with `integrations.tests_slack_member_all_devices` and the existing
bridge/privacy/owner-authority suites. The callback
concurrency regressions require real PostgreSQL row locks. Model drift checks
must show no new migration. Record actual run results separately; these labels
are not a passing-CI claim.

Before any production release, obtain approval for the reviewed exact source,
full actual-main CI and normal deployment, with the migration values above off.
Check the actual pending migration plan through the normal deployment guard;
this source change grants no new migration approval. After an approved release,
verify the actual runtime source/configuration and preserved baseline behavior
using the separately reviewed read-only probes.

Before first enablement, finish and release Phases 4b and 4c, release compatible
clients and complete the required device smoke checks. The owner-deferred
physical iOS build 95 check is not satisfied by this backend work. Obtain owner
approval for the exact user-app subscription changes, staff accounts, canary
scope, test operations and flag change. Verify fresh staff consent/scopes and
membership at that time, then run the approved public canary checklist. This
runbook contains no live callback, private-conversion, join/leave, key-removal,
posting or latency smoke result.

Rollback disables member expansion while retaining existing-room authority
reductions and the immutable mappings. Preserve recovery evidence and use a
reviewed forward fix for holds. The later Community cutover still needs seven
actual days of dual-source observation with deduplication; removal of old code
still needs fourteen stable days. Merges and test runs do not start or satisfy
either observation window.
