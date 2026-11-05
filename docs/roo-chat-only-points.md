# Roo awards to MLAI members without Slack

Manual awards can address an existing MLAI Chat profile directly. The wallet
remains `PointsAccount.user`; an unlinked recipient never creates a Slack
placeholder or requires Slack lookup.

`POST /api/v1/points/admin/award/` retains its Roo service authentication and
linked full Points Admin requirement. Submit exactly one of `target_slack_id`
and `target_profile_id`. Profile IDs are the public `community_chat_profile_id`
UUID, not an email, display name, device key or numeric user ID.

```json
{
  "admin_slack_id": "UADMIN",
  "target_profile_id": "00000000-0000-4000-8000-000000000002",
  "points": 4,
  "reason": "Helping at the community event",
  "idempotency_key": "roo-chat-award:verified-source-event:canonical-profile-uuid"
}
```

Profile awards require a stable `idempotency_key` of at most 200 characters.
The backend scopes operations to the configured community and canonical
administrator. Replaying an operation returns its ledger entry; changing the
canonical recipient, points or reason returns HTTP 409. A later Slack link to
the same recipient does not create a second wallet or change operation identity.
Legacy Slack callers may omit the operation key during the compatibility period.

Responses include `success`, `ledger_id`, `created`, `replayed`, `new_balance`
and a `recipient` object containing `kind: mlai_profile`, `profile_id`,
`display_name` and nullable `slack_user_id`. An HTTP 200 response must have
`success: true` before a caller reports an award. Balances belong in private
member surfaces, not public channel confirmations.

Canonical account rows are locked in primary-key order, before Points Admin,
device and wallet rows. The operation revalidates the current actor identity,
recipient account/device and full admin role. Positive awards compare canonical
accounts to prevent self-awards through aliases. The administrator's role row
serializes allowance checks and credits, including simultaneous commands.
Duplicate operations are checked before their already-consumed allowance.

For a Chat command, the authenticated bridge actor handoff supplies the verified
human and explicit native recipients. See [the bridge contract](mlai-chat-bridge-contract.md).
The model cannot supply recipient identities or the source operation key.
Release this backend contract before the matching Roo and client changes.

## Native introduction rewards

The existing Volunteer receipt pipeline can credit a Chat-only account without
passing through Slack's bot-excluding introduction listener. Enable only this
action with:

```dotenv
COMMUNITY_CHAT_VOLUNTEER_ENABLED=true
COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=false
COMMUNITY_CHAT_VOLUNTEER_INTRO_REWARDS_ENABLED=true
COMMUNITY_CHAT_VOLUNTEER_INTRO_ACTIVE_FROM=2026-10-25T00:00:00Z
```

The boundary above is an example, not a production activation instruction.
Configure the verified public `start_here` channel in
`COMMUNITY_CHAT_VOLUNTEER_CHANNELS`, retaining the existing help-channel mapping.
The Chat deployment's separate intro receipt collector must use the same approved
activation boundary, its own cursor/volume and only the introduction channel.
Neither changing a help-channel cursor nor enabling unrelated rewards is needed.
An empty, malformed, naive or future activation boundary cannot pay an earlier
post. The global awards flag continues to support the existing full programme.

Native introduction eligibility deliberately retains the existing objective
receipt policy: a verified member's original, nonempty, top-level post in the
configured introduction channel. This path does not call an AI classifier.
Threads, edits, imported echoes, revoked devices and other channels are excluded
by source normalization and admission. Admission also proves that the current
canonical author owned the verified device at the signed post's time. Delayed
receipts, ambiguous ownership changes within one timestamp second and missing
verification timestamps cannot credit a later owner of the same key.
Slack's existing introduction classifier
continues to apply to native Slack posts.

The reward is four Roo once per canonical member. Native receipts and the
legacy Slack first-contact award share the member lock and recognition outcome.
Adding another device or a Slack link does not pay again. Pending recognition
can be completed by an eligible first-contact award without creating another
once-only outcome; explicit review decisions are preserved.

Deploy compatible backend, Roo and client changes before activating the scoped
collector and gate. Historical recovery must use the original verified public
introduction and a bounded reviewed replay, after checking existing ledger and
recognition records. Do not issue an unrelated manual credit and then replay
the automatic introduction later.

## Native startup support and useful answers

Native support uses a separate `COMMUNITY_CHAT_VOLUNTEER_BOOST_REWARDS_ENABLED`
gate and explicit aware `COMMUNITY_CHAT_VOLUNTEER_BOOST_ACTIVE_FROM` boundary.
Configure `boost_startup` alongside existing `help` and `start_here` channel IDs.
Keep general awards and milestone bonuses unchanged. Set the explicitly reviewed
positive reaction list in `COMMUNITY_CHAT_VOLUNTEER_LIKE_REACTIONS`; an empty list
means no reaction qualifies. A literal native `done`, `boosted`, `liked` or `shared`
reply also qualifies. One canonical member receives two Roo once per root post,
across reactions, confirmations, devices, later Slack linking and existing Roo
link-love awards. Native-only accounting does not call AI or require AI consent.

Imported Slack roots require the exact stored public channel/message link and a
completed create delivery. A bridge tag or a bridge signing key alone grants no
authority. Self-support checks compare canonical Slack aliases. Native answer
rewards on imported questions similarly require the currently verified original
questioner's useful mark; the bridge key never acts as that questioner. Source-time
device ownership is checked for native actors, answers and native questions.
Reviewed, withdrawn and reversed outcomes remain intact on automatic retries.

The Chat startup collector uses a separate durable cursor and activation instant.
Historical repairs must be an independently reviewed, source-backed finite batch;
never rewind the advice or prospective startup cursor. Record its source proof,
prior ledger aliases, exact delta and resulting ledger IDs. The original Abraham
two-point replay is part of historical recovery, never an extra entitlement.

## Verification

`roo.tests_manual_awards` covers real wallet credits without Slack, private
balance reads, changed-payload conflicts, stale account snapshots, role and
allowance checks, legacy compatibility and PostgreSQL concurrency.
`integrations.tests_bridge_roo_actor_database` covers acknowledged member
posting and current device/grant/recipient revocation. Volunteer and intro
handoff suites verify action-specific activation and cross-platform deduplication.

These suites require an approved disposable migration plan; no schema changes
are introduced by this feature. Never run them against production data.

Older receipt formats without original target times retry with `409 source_time_required`
when a referenced signing key has changed account owners. The updated collector
supplies the original timestamp; its replacement owner never inherits the award.

Fixed two-point link-love ledger entries also classify contribution progress
before a native receipt arrives. Starting member history before either route
pays keeps new accounts known; mirroring the same ledger never counts it twice.
Existing withdrawals and corrections remain enforced across both routes.
