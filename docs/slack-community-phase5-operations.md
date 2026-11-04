# Phase 5: Community member posting and verified-account bot fallback

This source implements Phase 5 behind the existing
`SLACK_COMMUNITY_SEND_AS_MEMBER` switch. It records no release, production
configuration change or live canary result. It adds no migration, model,
consent version, scope or wire protocol. Phase 6 member-source intake and the
seven-day comparison window remain separate work.

New ordinary Chat-to-Slack Community work uses the author's own current linked
grant when its consent, scopes, exact verified/unrevoked device and joined
membership are eligible. Selection propagates quota and transport uncertainty;
malformed membership proof, selected-authority races and uncertain credential
refresh defer. Definitive absence of an eligible grant can
choose the bot. The bot requires the exact authenticated signing device of an
active account and current AI consent, without a Slack identity link or channel
membership. New Phase 5 content always requires AI consent, even when the
legacy bot consent toggle is disabled. It takes its displayed name from that account's MLAI profile. A
claimed payload name or user ID does not confer authority.

Community admission has no per-account device rank/count restriction and no
Members audience union or acknowledgement requirement. Members retain their
complete all-verified-device union, 1,000-key ceiling and exact CAS receipt;
owner-private rooms retain their separate eight-device policy. The existing
Members fences, personal/Discord paths and explicit coworking command remain
separate.

## Scope and feature-off behavior

The initial migration switches remain `false`, `false`, `bot`, `false` for
Members, Community member posting, Community member-source intake and private
event routing respectively. This source does not set them. Preserve their
approved live values independently of documentation.

New Phase 5 permission additionally uses the existing configured workspace and
`SLACK_MEMBER_CHANNEL_ALLOWED_IDS` cohort. Its exactly empty value means an
otherwise enabled unrestricted rollout. A canary therefore needs an explicitly
approved Community channel ID, preserving any separately approved Members
scope. Malformed/out-of-scope values deny new Phase 5 admission, including the
new unlinked bot permission. No fifth boolean is introduced. Existing ordinary
legacy bot behavior outside that scope remains subject to its previous linked
AI gate; this prevents one-channel testing from enabling D2 across fifteen
channels.

With the switch off, the legacy bot consent resolver keeps its prior linked
behavior. Existing server-recorded commands and original-poster cleanup still
take the owned executor. Off or narrowed scope stops new content mutations;
an actual previously armed attempt may perform read-only completion
reconciliation. Cleanup uses current device/account/grant/source authority and
the original poster. It never gains permission to post new content after AI
withdrawal. No scope, AI, lease or token failure switches an existing member
command to a bot.

## Durable actor and request identity

Only a new server-marked enqueue can start a Phase 5 command. A short metadata
transaction saves its mapping, source intent and deterministic client UUID
before selection probes. Actor, profile-derived bot header and complete wire
request then commit before the mutation attempt is armed in a separate commit.
Retries keep those exact bytes and captured authority. A changed consent epoch
or device cannot rewrite a saved actor. A fresh mutation may use current
authority for the exact stable original member account; new bot edits remain
bot even if that author subsequently links Slack.

Legacy unstamped queued work has no durable proof that a bot request was
definitely unsent. Enabling Phase 5 retains such work on a five-minute retry hold rather than
relabeling or blindly resending it. Before enablement, the operator must inspect its bounded
queue impact and resolve individual uncertain entries under a reviewed plan.
FAILED/dead status alone does not prove rejection. This source performs no
backfill, replay, deletion or operational repair.

The provider transaction locks User, grants, connections, devices,
memberships/identity/roles before State, Channel, Delivery and target links.
It rechecks current source mapping, exact poster and target, consent and lease
through bounded I/O and settlement. Community tokens omit Members relay scope.
Bot SDK instances are per operation, use the normal shared budgets, disable
automatic retries and recheck the lease after quota admission before actual
SDK entry. No callback is installed on the shared legacy singleton.

Accepted-but-unproven results remain ambiguous. Exact response facts are
retained even after the original lease expires without overwriting a newer
command/result. Read reconciliation requires the original actor and exact
target/client ID/text evidence plus public/non-Connect source checks; absence
does not authorize a resend. A previously acknowledged bot response may
settle from its exact command-bound closed receipt; this exception cannot
authorize a Members row or fabricate missing actor evidence. A client UUID is not assumed to provide Slack
deduplication. External Slack conversion after the last info proof cannot be
atomically serialized by local database locks and remains a canary limitation.

Edits/deletes and reaction removal use the original stored poster. Member B's
reaction on member A's message uses B's token and exact reaction link. Bot-only
authors still share Slack's one bot reaction identity; this source does not
claim to distinguish those external bot reactions. Imported/native reaction
ownership remains unchanged. Echo checks recognize real Community commands and
attempts, preserve current-edit and reaction-revision ordering, and never treat
an unattempted command as proof of a send. Exact authenticated reaction
observations still order older callbacks after scope rollback, including
receipts retained by the legacy bot intake. Server-only command fields are
removed from untrusted metadata.

## Reading held and dropped sends

A held send stays `pending` with a later `available_at` and does not use up a
provider attempt. Its `last_error` is a fixed code that says why it is held:

| `last_error` | Meaning |
|---|---|
| `provider_budget_deferred` | Waiting for Slack quota (shared budget or 429). |
| `community_bot_reader_unavailable` | A bot post needs a joined member reader for the channel, and none is current. |
| `community_bot_reader_scope_unavailable` | The current reader grant lacks a read scope the bot post needs. |
| `legacy_community_send_unproven` | Legacy hold: the row has no enqueue marker, so it is retried every five minutes and never resent blindly. |
| `community_channel_not_admitted` | The channel is out of the current workspace or channel scope, or processing is paused. |
| `community_mapping_pending` | The room mapping or sync state changed, or is paused, while the send was running. |
| `community_member_access_disabled` | A member-room send (either direction) or a member-actor Community post is waiting because member access is switched off. |
| `community_receipt_unverified` | Slack's response did not yet prove the expected poster. |
| `community_source_closed` | The source channel was reduced while the send was running. |
| `member_membership_unproven` | The author has a linked grant, but their channel membership could not be confirmed yet. |
| `member_delivery_deferred` | Any other hold from the shared send machinery (reconcile reads, response checks) that has no specific code, for member or bot posts. |

A reply whose parent has no Slack message yet is a separate dependency wait:
its row status is `waiting_parent`, not `pending`, until the parent is mapped.

Permanent eligibility failures still make the row `dead`, and `last_error`
keeps the exception text (for example `PublicBridgeConsentRequired: Current AI
sharing permission is required`).

The delivery row (`status`, `last_error`, `available_at`, `attempts`) is the
source of persisted state. The `slack_member_send` log line is a best-effort
classification of one attempt, written before the worker saves the row's new
status. Logging can be skipped (for example with a closed database connection,
or a transaction that does not commit) and logging failures are ignored, so a
missing line does not prove that no attempt ran.

When a line is written, it carries the `delivery_id`, an `outcome` and a
`reason`. This covers failures during actor selection, before any Slack call.
The line has `poster=member|bot` only after a poster was chosen. A legacy hold
or a mapping change found during the first room-mapping check has no log line;
read it from the row's `last_error`. Outcomes, and the row state they usually
lead to:

- `settled`: posted; the row becomes `completed`.
- `deferred`: held. `reason` is the same code the row's `last_error` records,
  or `parent_mapping_pending` for the `waiting_parent` dependency wait above.
- `dropped`: permanent; the row becomes `dead`. `reason` is `consent_required`
  (no current AI-sharing consent), `device_unverified` (the signing device is
  not a current verified device of an active account), or `permanent_failure`.
- `unavailable`: permanent `MemberDeliveryUnavailable`; the row becomes `dead`.
- `retry`: provider outcome uncertain. With durable sync (`MESSAGE_SYNC_ENABLED`)
  the row becomes `failed` with backoff. Without it, the row becomes `dead` once
  it reaches its attempt limit.

Read the delivery row by `delivery_id` first, and use these log lines to see
why a missing Chat-to-Slack post was dropped or held. Log lines never contain
message text, names or tokens.

## Validation and later gates

The owning label is `integrations.tests_community_member_send_phase5` in both
canonical SQLite and PostgreSQL lists. Existing
`integrations.tests_public_bridge_consent`, `integrations.tests_slack_member_access`
and `integrations.tests_member_delivery_phase4c` remain regression gates.
`integrations.tests_community_delivery_observability`, also in both lists,
covers the hold codes and send-log outcomes above without a database.
The new PostgreSQL-only interleavings exercise device/AI/grant revocation
through send and settlement. Database tests require the specifically approved
existing migration files; this document grants no migration permission or
validation success claim.

Release and canary steps require a reviewed exact source, full hosted CI,
current native merge gates, owner release approval, normal deployment and
source/schema/Studio/services/flags checks. Before enabling, review legacy
queue holds, verify the exact one-Community-channel cohort and the two current
staff authorities, and prove real Slack attribution, member/bot mutations,
echo handling, consent/device revocation and rollback. Only after that canary
may the owner enable the remaining Community channels. Client/physical smoke
and Phase 4 live/privacy gates remain separate; no elapsed observation window
is claimed here.
