# Server inbox rollout and rollback

The source implementation is complete behind default-off flags. Sam authorized
direct production testing and administrator merges on 11 October 2026,
superseding the staging and pre-activation shadow requirements below for this
release. Follow Chat's `docs/mlai/INBOX_PRODUCTION_TEST_PLAN.md` and record actual
deployment and acceptance evidence. This document itself is not a deployment
record. The backend introduces no schema change for this feature.

The deployment workflow now supplies every inbox flag from repository variables
with default false and the dedicated account secret from GitHub secrets.
The release validates activation inputs before changing host configuration.
Managed secret installation refuses silently replacing an existing account
secret, and managed flag writes reject malformed/multiline values. Deployment
does not automatically bind the whole member population.

## Preparation

Deploy reviewed relay schema and code with inbox disabled. Backfill the relay's
display-time projection in bounded batches, retain retired trusted bridge keys,
and verify the projection before advertising inbox support. Deploy the private
membership and bridge adapter code with their new capabilities disabled.

Configure one stable `MLAI_CHAT_ACCOUNT_KEY_SECRET` on every backend web and
maintenance process. Follow [account binding](mlai-chat-inbox-accounts.md) to
preview and then apply verified-device bindings in bounded pages. Do not rotate
this secret or remove the backend user ID while a deletion target still needs
either value to locate historical grouped and singleton cursor rows.

Before enabling account cursor storage for real members, configure the separate
relay cursor deletion target and its operational owner. It requires verified
chat access revocation, `inbox_erasure_v1`, and zero remaining relay cursor,
export and revision rows. It does not complete the whole account receipt or
replace bridge-copy, message, media, log or backup deletion boundaries. See
[account privacy](community-chat-account-privacy.md).

## Backend flags and order

All flags below default to false. The relay and adapter capability gates must
agree on the same community; flags alone cannot manufacture capabilities.

| Flag | Enablement prerequisite | Rollback effect |
| --- | --- | --- |
| `COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED` | Reviewed opaque account grouping and matching membership adapter | Stops new binding writes; retains existing bindings and revocation fences |
| `COMMUNITY_CHAT_INBOX_ERASURE_ENABLED` | Revocation cleanup owner, private erasure capability and verified deletion receipts | Pauses this deletion worker; pending targets remain pending |
| `MESSAGE_SYNC_INBOX_MENTIONS` | Trusted bridge signer and mention-capable adapter | New envelopes omit optional mentions; already frozen envelopes retain their identity |
| `MESSAGE_SYNC_INBOX_CURSOR_PUSH` | Verified relay projection, bindings and source/export fence recovery | Stops source pushes; retains the legacy source snapshot path |
| `MESSAGE_SYNC_INBOX_READ_EXPORT` | Controlled bidirectional Slack acceptance and client cohort review | Stops export handling; retains relay cursors and durable retry state |
| `MESSAGE_SYNC_TARGETED_READ_POLLING` | Controlled provider-budget and freshness acceptance | Restores legacy probe selection and freshness criteria |
| `MESSAGE_SYNC_RELAY_READ_COUNTS` | Supported clients already use relay counts and each mapped room has current cursor capability context | Restores mapped-room history probes; source-only inventory is unchanged |

First run relay shadow comparisons. Use a dedicated staging community, worker
configuration and controlled Slack workspace to exercise source push, exports
and targeted polling together. Global flags in that environment must not affect
real members. Exercise all acceptance scenarios in the Chat design plan,
including unread regression, a stale observation after a device read, revoked
devices, temporary provider failures and permanent fence settlement.

Review at least three days of aggregate shadow results before changing unread
authority for a real account. Then expand internal preview clients, the reviewed
TestFlight cohort and supported clients in the Chat runbook's order. Production
exports and targeted polling follow that cohort review; relay-owned mapped
counts follow supported-client adoption. Local synthetic tests do not establish
Slack latency objectives or replace this human review.

## Validation and cleanup

An exported native anchor waits while its bridge create is pending, processing,
waiting for a parent, or failed with a retry still possible. Completed or
permanently dead deliveries cannot produce a new link: when their link is
absent, resolve the latest inbound Slack message at or before the relay
frontier. A consent-rejected native message therefore does not block the
durable export page or later cursor revisions indefinitely. This does not
retry or bypass the rejected message's consent check.

A native message's Slack delivery time may be later than its relay display
time. After the exact exported write confirms, retain a consent/OAuth-scoped
native boundary receipt in the existing connection cursor. A probe with that
settled revision maps only the native Slack timestamp and the microsecond
immediately before it to their corresponding relay display frontiers. The
confirmed write's echo is an observation, so it preserves the device anchor;
an external backwards change remains a regression. Raw Slack timestamps stay
in polling/coalescing state. Unrelated timestamps, stale probe fences, changed
authority, and permanently abandoned writes are never translated. Disconnect
erases the receipt while retaining the revision fences. This prevents a native
unread echo from moving past the message and making a later device read unable
to clear manual unread.

Backend implementation checks use `scripts/test_without_database.py`; local
model dry-runs reported no changes. Database-backed Django concurrency tests,
controlled Slack acceptance and deployment remain separate approved gates.
Preserve provider admission, Retry-After deadlines, OAuth/consent generation
checks, per-target fairness and reserved safety-sweep capacity during rollback.
`READ_STATE_SAFETY_SWEEP_HOURS` defaults to six; targeted freshness measures
possibly-unread conversations rather than archive completion or cache size.

A human's root post or ordinary reply in an enabled public Slack-to-Chat
mapping prompts an owner read probe even when Slack's event authorization
list contains only the bridge installation. The hint stores only the source
room ID and does not mark anything read. Current owner grant, consent, scope,
membership and device access are still checked by the worker. This author
fallback requires explicit `channel_type=channel`; private rooms, bots,
unmapped rooms, edits and deletes retain the explicit-recipient path.
Explicit eligible owner/visibility hints precede older unhinted cache refresh
work. Age gates, per-target Retry-After deadlines, oldest-first ordering within
each tier and the reserved one-in-ten safety turn remain unchanged.
Own-message hints precede visibility hints so an older open row cannot delay
the owner's new read-position probe. A routed Chat room remains mapped when
the worker attaches owner-directory activity metadata; only a target whose
channel ID is the Slack source ID is source-only. Mapped targets retain relay
cursor capture, confirmation and baseline checks under the current authority
and consent fences. Source-only targets never create relay cursors.

After two stable weeks and a raised minimum supported client version, remove
legacy client channel read merges, forced-unread stores, unread-history fetches
and channel NIP-RS frontiers. Keep thread contexts until the server thread
cursor design is implemented. Keep the backend snapshot API while supported
older clients still require it. No compatibility path is removed by this stack.
