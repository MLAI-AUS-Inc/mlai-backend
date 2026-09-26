# Slack conversation opening latency — 25 September 2026

Read-only inspection of production web/bridge-worker logs, bounded connector
metadata, delivery status aggregates and content-free provider counters. Database
queries ran in a read-only transaction. No credentials, message bodies or
production mutations were involved.

The running backend release was
`938762226a42a79d421da3942dc6a145b00329d5`. Times below are UTC; Melbourne was
UTC+10 on this date.

## Observed delays

- A sequence of open requests returned `202` from 13:17:18.080, then `200` at
  13:18:47.867: approximately 90 seconds. Most individual HTTP requests took
  116–729 ms; one took 4.96 seconds. The successful response took 127 ms.
- A second sequence started at 13:19:11.003 and remained `202` through
  13:22:10.807. The server had accepted the selected source and provisioned its
  existing mirror by 13:19:16.607, about five seconds after the first response.
- An inspected new mirror was created at 13:18:20.854. Its registration was
  created at 13:18:22.183, acknowledged at 13:18:34.938, and its history scan
  completed at 13:18:43.660. These times strongly associate it with the first
  opening sequence, but HTTP logs omit source IDs, so this correlation is an
  inference. On that interpretation, approximately 63 of the 90 seconds passed
  before the local mirror existed.

The long wait is therefore primarily asynchronous provisioning/import readiness,
not a single slow browser request. Client backoff adds at most its polling delay
once the server is ready.

## Already imported conversation incorrectly blocked

The second selected mirror was live and had current source-coverage proof,
completed history, 193 completed message creates, 25 completed edits and 31
completed reactions. Its catalog still reported pending import work, preventing
opening.

The sole effective blocker was a dead backfill deletion retained after an older
room was reset. Its exact error was `Private conversation participants changed`.
The later device transition had rebound its participant metadata, but this was
still a cancellation tombstone from the old room. The target message had been
successfully imported into the current room. The existing refresh-status code
already excluded this exact class of cancellation; catalog readiness did not.

The prepared catalog correction uses the same precise exclusion. Genuine
current-room pending, failed and dead deletions continue to block initial
publication. It neither replays the cancelled deletion nor modifies stored
production messages.

## Repeated source validation competes for shared capacity

Every retry of a pending open reran `conversations.info` before entering the
resumable membership/profile checkpoint. A later member/profile deferral could
therefore discard successful source validation and make the next attempt
compete for that same provider slot again.

The busiest provider scope showed:

| UTC minute | info admitted / deferred | history admitted / deferred |
| --- | --- | --- |
| 13:17 | 35 / 48 | 44 / 154 |
| 13:18 | 34 / 52 | 42 / 133 |
| 13:19 | 35 / 39 | 43 / 147 |
| 13:20 | 33 / 37 | 41 / 146 |
| 13:21 | 31 / 38 | 42 / 150 |

No upstream Slack rate-limit responses were recorded in these observed counters;
inspected durable cooldowns were null. These are shared provider-scope counters,
not attribution to an individual open. The deployment still had substantial
local admission contention.

The prepared source-checkpoint change retains a successful validation for at
most 60 seconds in the existing directory-progress record. That record is
scoped to the selected request, owner, source, OAuth/consent generation and
history window. A changed room-membership fingerprint invalidates it. Only
bounded conversation identifiers, names, flags and the activity timestamp are
retained; `latest`, message bodies, topics and arbitrary provider fields are
discarded. Every retry rechecks current account/device authority, and provider
quotas and Retry-After remain enforced.

This reduces duplicate work without promising a fixed opening time: first
imports still depend on Slack admission, relay provisioning and actual message
delivery. Post-deployment measurements are required to quantify the improvement.

## Local validation

The 18 foreground-open tests passed using `scripts/test_without_database.py`,
which forbids database and network access. Regressions cover reuse after nested
provider deferral, expiry, membership invalidation, exclusion of message payloads,
device/consent fences and preservation of provider backoff. No database migration
or operational repair is part of this investigation.
