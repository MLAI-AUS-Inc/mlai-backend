> Historical rollout document. Bot/both modes and direct replay tools are retired;
> see [permanent operations](slack-member-permanent-operations.md) and
> [bounded member repair](slack-member-repair.md) for the supported contract.

# Phase 4b: member-authorized public history

This source adds history, head repair and reply pagination using a current member's Slack login. It applies to Members rooms and to Community rooms when `SLACK_COMMUNITY_SOURCE=members`. Members never fall back to a bot token, including when member switches are off. Community `bot` and `both` history and owner-private history retain their existing paths. No schema, consent choice or production configuration changes are included.

## Current authority and page commits

Reader selection and any necessary OAuth refresh happen before the page transaction. A page locks its sorted joined User cohort, all their grants and Slack connections, their devices and memberships, then its State, Job and Channel. The selected reader must still have the exact captured Slack subject, connection, token generation, accepted existing public-channel consent and a current joined observation after the membership epoch. AI consent and an identity attribution link are not read prerequisites.

The source must be a fresh, public, non-archived, non-deleted, locally approved mapping without a known owner-private registration. A Members room additionally includes every current verified, unrevoked device
of its active consenting joined accounts, within the 1,000-key room cap. It
requires the complete current device-key hash and the exact adapter-acknowledged audience generation/room identity, with neither room nor audience pending. New authority not present in the locked cohort causes a retry rather than a later User lock.

A bounded, budgeted `conversations.info` call must affirm the exact public, non-Connect source and literal `is_member=true` before history or replies begins. A second INFO proof is required after the content response, before any body returns to the page writer or persists. Missing or malformed evidence defers; only literal `is_member=false` records a departure. The same local authority locks remain held through both proofs, the content request and atomic outbox/coverage/checkpoint commit. A fast content read may finish inside INFO's shared 1.2-second budget interval. Only a pre-request `conversations.info` admission refusal of at most three seconds receives one bounded wait outside the budget connection; the lease is checked before and after that wait and again after admission. A second deferral, longer cooldown or provider 429 discards the page and reschedules. There is no new reader/token or quota override. The exact lease is checked before each request, again after quota admission immediately before SDK I/O, and at commit; an expired or superseded page discards its response. SDK retries remain disabled. The existing app/workspace/method budget commits separately, so rolling back a page does not refund provider quota.

Slack can change membership or privacy outside the database after the final INFO proof. This fence serializes local consent/device/membership changes, not external Slack administration. Contradictory source metadata in a history response is rejected, and normally unannotated history/replies bodies require the second INFO proof. The later approved canary must test live event and conversion behavior; this source has no such live evidence.

## Reader changes and recovery

Auth or channel-access failures are recorded only after releasing page locks, against the captured authority. A known departure uses the existing membership reduction and audience synchronization lane. Members stay closed until metadata selects a current reader and a full real audience synchronization acknowledges current authority; a replacement reader cannot bypass a pending room. Budget/transport deferrals do not prove departure. Candidate credential failures can try another reader within a bounded pass. There is no member-to-bot fallback.

When no current eligible reader exists, `sync_paused_reason=no_reader` and an empty reader pointer are persisted. The recurring job and existing membership/metadata wake can recover when a reader returns. This does not archive the destination or claim Slack history is exhausted.

Every member checkpoint is bound to a content-free hash of State generation, source and destination identity, selected grant/user/connection/Slack subject, consent and OAuth generations/scopes, membership epoch, source reduction observation, and (for Members) the acknowledged audience generation/hash. Reader, consent, source, room or audience changes restart pagination rather than transplanting a cursor. Accepted membership departures invalidate that source's member-history State generation before Channel locks, fencing leave/rejoin ABA even when the same reader returns. Switching Community back to bot/both also discards the member cursor. Recent-first scroll demand uses the same chosen-reader scope and checks Members requester membership/device entitlement before scheduling; it performs no provider I/O.

## Legacy repair commands

The direct bot `get_channel_history` and `get_thread_messages` helpers refuse member-governed mappings. The legacy commands `repair_community_bridge_slack_thread`, `reconcile_community_bridge_slack_threads`, `backfill_community_bridge_thread_references` and `repair_community_bridge_threads` likewise refuse these mappings before reading or replaying them. Their existing Community bot/both behavior remains available. Member history and observed-content/reaction repairs run as leased archive/head/thread jobs; direct operator replay is not an alternative authority path.

## Verification and rollout holds

Canonical CI assigns `integrations.tests_message_sync_member_history_phase4b` to SQLite and PostgreSQL. The `postgres-only` interleaving tests require real row locks. Relevant baseline modules cover private/public history, recent-first and head repair, observed corrections, member access/membership/audiences and legacy repair commands. Run only against a disposable database using the explicitly approved existing migration inventory; see [backend runtime](backend-runtime.md) for normal commands and required CI. Synthetic adapters are not production acknowledgements.

This is a feature-off release candidate. It does not enable a switch, change Slack app subscriptions, run a live smoke test, or deploy production. Phase 4c must supply the current-authority outgoing delivery fence and actor-bound posting/edit/delete behavior before activation. Backend release, any later flag/app changes, live canary actions, physical iOS build 95 smoke, the seven-day Community dual-source period and the fourteen-day deletion gate retain their separate owner/time requirements. A restricted Slack distribution still admits only the existing one-history-call-per-minute allowance; this change promises no faster quota.
