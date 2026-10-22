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

## Validation and later gates

The owning label is `integrations.tests_community_member_send_phase5` in both
canonical SQLite and PostgreSQL lists. Existing
`integrations.tests_public_bridge_consent`, `integrations.tests_slack_member_access`
and `integrations.tests_member_delivery_phase4c` remain regression gates.
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
