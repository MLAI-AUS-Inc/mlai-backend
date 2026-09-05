# Volunteer account API

Volunteer lives under `/api/v1/community-chat/volunteer/`. It reuses account
sessions and the existing user JWT. Bootstrap and reporter credentials cannot
award or review. Community scope derives from deployment configuration and
cannot be supplied in a request. Reads and writes are disabled by default.

## Wire types

All Roo amounts are exact decimal **strings**, with up to six fractional digits.
The database and existing PointsService use integer microroo. Do not parse these
amounts into binary floating point to calculate credits.

`Member`: `id`, `display_name`, nullable `avatar_url` and `public_key`. IDs identify
canonical MLAI accounts. The optional key belongs to an active, verified,
unrevoked Chat device and supports existing profile/direct-message navigation;
no key is advertised for an inactive account. No private email, Slack identifier
or wallet data is included in member search.

`Source`: optional/null `channel_id`, `thread_root_id`, `message_id`, `source_id`,
`event_id`, `url`. The configured channel allowlist contains public community
destinations only. Unconfigured/restricted sources fail closed. A source URL
is never fetched by the backend. Existing app thread navigation stays canonical.

`Level`: `key`, integer `level`, `name`, `threshold_roo`, `bonus_roo`, `pathway`.

`Action`: `key`, `title`, `description`, `reward_roo`, `reward_max_roo`,
`requires_attendance`, `period`, `cap`, `cap_group`, `verification`, `channel_key`,
`repeat_label`, `completed`, `eligible`, `unavailable_reason`, `completion_id`,
`recognition_status`, `source`, `opportunity_id`. Completion derives from
approved source-backed records. Pending work never appears as a completed tick.

`Opportunity`: `id`, `kind` (`event`/`project`), `action_key`, `title`, `purpose`,
`description` (definition of done), `learning`, `guide`, `reviewer`, `source`,
nullable `event_id`, `project_id`, `starts_at`, `ends_at`, `reward_roo`,
`reward_max_roo`, `recommended_level`, `requires_attendance`, `status`, `version`,
`can_request`, `guide_available`, `guide_is_fallback`. Closing an event leaves its
recognition path available. An unavailable guide resolves to a reachable
authorised reviewer or the configured fallback reviewer. If none is reachable,
the original name is preserved with `guide_available: false` and no contact key.

`Project`: `id`, `title`, `purpose`, `description`, `guide`, `source`, `published`,
`version`, `opportunities`, `guide_available`, `guide_is_fallback`. Only curated
public briefs belong here; guide fallback uses the configured reviewer.

`Contribution`: `id`, `action_key`, `title`, `definition_of_done`, `member`, `opportunity_id`, `source`,
`status`, `credit_status` (`credited`/`not_awarded`/`reversed`), `note`, `evidence`,
`reviewer`, `reward_roo`, `reward_min_roo`, `reward_max_roo`, `bonus_roo`,
`occurred_at`, `created_at`, `updated_at`, `version`, `review_history`,
`can_resubmit`, `can_withdraw`, `can_review`. The reward range is the agreed
snapshot, not today's edited opportunity. Review history entries have
`decision`, `note`, nullable `actor`, `automatic`, and `at`. Preserve member
`resubmitted`/`withdrawn` entries as such; they are not reviewer feedback. A
deleted or newly restricted source is returned as `{}` while the private
recognition history remains available.

## Member routes

| Route | Contract |
| --- | --- |
| GET `journey/` | `account_id`, `community_id`, `relay_url`, `policy_version`, `contribution_roo`, `wallet_balance`, `current_level`, `next_level`, `levels`, `points_to_next`, `progress`, `attendance`, `suggestions`, `actions`, `capabilities`, `feature_flags`, `history_reconciled`, `updated_at` |
| GET `policy/` | `{version, levels, actions}` |
| GET `opportunities/`, `opportunities/:id/` | Public opportunity list/detail; optional kind/project filter |
| GET `projects/`, `projects/:id/` | Public project list/detail |
| GET `contributions/` | Personal list; `filter=conversations\|awaiting_review\|recognised` |
| GET `contributions/:id/` | Own private contribution receipt |
| POST `requests/` | `{action_key, opportunity_id?, source, note, evidence?, idempotency_key}` |
| POST `contributions/:id/resubmit/` | `{version, note, evidence?}` |
| POST `contributions/:id/withdraw/` | `{version}` |

Lists return `{results, next}`. `next` is a same-path query containing
`offset`/`limit`, or null. Default page size 20, maximum 50. Conversation entries
use Contribution shape with `status=conversation`, no earned points or writable
capabilities, and a verified source/opportunity. They link to that thread or
opportunity; their IDs are source-receipt IDs, not recognition receipt IDs.

`progress` has exact `earned_roo`, `required_roo`, and a presentation `fraction`.
Amounts/rank/progress are **null** when historical contribution classification is
unreconciled. Wallet balance remains separately available. Clients must show
pending reconciliation, never silently turn these nulls into Level 0.

Clients compare the trusted `relay_url` with the active community before
showing Volunteer data. Normalize ws/wss host and path while preserving ports;
display a switch-community state on mismatch. Never send a body community
override to make a different relay appear to own the MLAI account's Roo data.

`capabilities`: `can_review`, `can_publish`, `can_correct`, `can_request`.
`feature_flags`: `enabled`, `awards_enabled`, `bonuses_enabled`. Submission
enablement is independent of credit enablement. Pending submissions earn nothing.

## Committee routes

| Route | Input / result |
| --- | --- |
| GET `manage/reviews/` | Default own/fallback queue; `scope=all` is available to authorised reviewers |
| GET `manage/contributions/:id/` | Authorised private receipt detail |
| POST `manage/contributions/:id/decision/` | `{decision, note, version, idempotency_key, reward_roo?}` |
| POST `manage/recognitions/` | Request fields plus `member_id`, `feedback`, optional `reward_roo`; resolves existing outcome |
| POST `manage/events/:event_id/recognitions/` | `{recipients:[{member_id,reward_roo,note}],idempotency_key}`; maximum 50, initially unselected in clients |
| GET `manage/members/` | `q` matches public display names, minimum two characters; or exact verified `public_key`; at most 20 linked active accounts |
| GET/POST `manage/opportunities/`, GET/PATCH `manage/opportunities/:id/` | Opportunity fields, with `guide_id`/`reviewer_id`; PATCH requires current `version` |
| GET/POST `manage/projects/`, GET/PATCH `manage/projects/:id/` | Project fields, with `guide_id`; PATCH requires current `version` |
| POST `manage/attendance/` | `{member_id,event_id,checked_in_at,source_id,reason}`; audited real missed-scan correction |
| POST `manage/reconcile/` | `{member_id,historical_roo,ledger_cutoff,reason}`; authorised reviewed opening; no implicit bonus backfill |

Decisions are `approve`, `needs_update`, `not_approve`, `reverse`. Approval is
synchronous: canonical recognition, base ledger credit, stable milestone rows
and wallet-only bonus ledger credits commit together. A receipt says credited
only after the ledger is present. First human approval needs personal feedback;
automatic intro does not consume that rule. Reviewers cannot approve themselves.

Mutations return `{outcome, contribution}`. Batch returns per-member
`{results:[{member_id,outcome,contribution?,error?}]}` and retries safely. Duplicate
outcomes resolve to the original result; stale conflicting versions return 409.
Canonical content cannot earn a second category even if channel configuration
aliases map introductions/monthly updates/general posts to the same channel.
Authoritative introduction sources are reserved before human submission, so a
pending generic request cannot capture an automatic introduction award.

The recognised feed also includes standalone paid milestone receipts when no
contribution already carries that bonus. These use the milestone UUID with
`record_type: "level_bonus"`, `action_key: "level_bonus"`, `reward_roo: "0"`
and the actual ledger amount in `bonus_roo`. They are read-only and have no
request/review/resubmit actions. Personal detail uses the existing
`contributions/:id/` route; authorised reviewers use
`manage/contributions/:id/`. A historical approval's actual reviewer and reason
are projected from its audit receipt when available. Unknown actors stay null.
Linked bonuses remain on their original contribution and never appear twice.
The merged feed reads only a bounded recognition window plus at most six
standalone milestones, with filter-preserving continuation URLs.
Errors have `error` and `outcome` codes; common codes include `cap_reached`,
`attendance_required`, `source_unavailable`, `awards_disabled`, `conflict`,
`not_authorised`, `personal_feedback_required` and `reviewer_unavailable`.

## Source ingestion

POST `internal/receipts/` accepts only the separate
`COMMUNITY_CHAT_VOLUNTEER_RECEIPT_TOKEN` bearer, never an end-user token. Payload:
`{source_key,kind,origin,actor_public_key|actor_id,source,occurred_at,metadata}`.
Relay origin permits `post`, `reply`, `reaction` and requires a verified device
key. Luma permits `attendance`; the existing monthly pipeline permits
`monthly_update`; fulfilment integrations permit `merch`.

Metadata permits bounded `original`, `top_level`, `has_text`, `service_account`,
`invalidated`, `reaction`, `target_public_key`, `company_id`, `ledger_id`,
`checked_in_at`, `fulfilled`, `refunded`. Message bodies are not replicated.
For reactions, `source_id` names the canonical target post while `source_key`
names the reaction event; toggling or multiple devices cannot multiply awards.

Relay `invalidation` is a separate immutable receipt keyed by deletion event
plus original target ID. `source.source_id` is the deleted logical message ID;
metadata `deletion_kind` is `5` for an author deletion or `9005` for moderator
deletion. Kind 5 may omit channel when relay deletion removed that context and
can invalidate only the same canonical author's source. Kind 9005 requires an
allowed public channel and current Points Admin authority at ingestion.
`target_public_key` is optional when the deleted source is unavailable.
Wrong-author and unauthorised-moderator receipts are stored terminal ineligible;
they never invalidate work. Valid invalidations block pending recognition and
deferred automatic awards without rewriting original evidence or reversing an
already committed ledger. Reversal remains an accountable review decision.

The relay producer verifies signed, persisted events and canonical origins
before calling this service. Ordinary client events and clicks are not evidence.
Unlinked members receive terminal `202 ignored`; transient processing failures
retain a durable pending receipt. Source replay conflicts fail closed.

`retry_volunteer_receipts --limit 100` processes pending receipt rows. The
existing startup-update award calls the canonical member/month coordinator only
when awards are enabled; otherwise the legacy path remains unchanged. Its
configured reward (currently default 20) is retained. Existing company/month
ledger records are mirrored, never credited twice.

`sync_volunteer_attendance --event-id ...` uses the existing Luma client and
requires attendance enablement. It maps verified account emails and linked
devices, including actual ticket check-in timestamps. It never treats tickets,
registration, approval status or QR images as attendance and does not write Luma.
Run commands only in an explicitly configured, authorised environment.
An authorised missed-scan correction also records durable organiser evidence
and uses the same once-per-member first-attendance recognition. It still obeys
the awards switch; repeated corrections cannot mint another first attendance.

Conversation pagination groups persisted eligible replies by public opportunity
root before applying limits. Continuation URLs preserve the selected filter;
many replies in one conversation cannot hide an older joined opportunity.

## Disposable validation

`scripts/test_volunteer_disposable.py` requires an explicit approved migration
argument and a synthetic loopback connection JSON. It never loads `.env`,
refuses an existing test database, applies only the selected migration targets'
dependency closure, and removes the database it creates. PostgreSQL concurrency
tests use independent connections and barriers to exercise overlapping
recognitions, approvals and shared monthly limits. Additional current-model
test prerequisites are documented in `volunteer-schema-proposal.md`; do not run
their migrations without the approval described there.

## Rollout and historical treatment

All feature/recognition/award/bonus flags default false. Configure canonical
public channel IDs, supported like semantics, fallback reviewer, campaign start
and durable source workers first. No seed creates fake events or accounts.

New empty accounts start at zero; unknown legacy earnings remain unreconciled.
State initialization and journey accounting reads hold the same canonical
member row lock as issuance, so the first page load cannot race a first award
into an incomplete historical opening or miss its milestone.
An audited historical opening must be reviewed before enabling historical
bonuses. No data migration resets balances or replays old introductions. Level
milestone uniqueness excludes policy version; corrections/re-crossings cannot
mint bonuses twice. Reversal recovers remaining earned balance only, preserving
purchased credits and avoiding inferred debt; it removes the invalid qualifying
contribution from rank while retaining audit history.
Levels at or below an audited historical opening are also skipped during later
new awards, preventing an indirect historical bonus backfill. Journey levels
include `bonus_awarded` from an actual milestone ledger and `bonus_eligible`
for eligibility beyond that opening; rank thresholds alone never imply payment.

Historical bonuses require a separate, explicit operational approval. The
`backfill_volunteer_bonuses` command defaults to a read-only member proposal:

```sh
python manage.py backfill_volunteer_bonuses --member-id MEMBER --reviewer-id REVIEWER
```

An authorised operator may run `--execute` only after specific approval of that
proposal, supplying its `--expected-opening-roo`, `--expected-ledger-cutoff`,
`--expected-state-token`, each explicit `--approved-level level_N`, and a
`--reason`. The reviewer must currently have `can_correct` and differ from the
beneficiary. The opening must have an accountable reconciliation. A changed
opening, cutoff, reconciliation snapshot, current contribution total or policy
rejects the old proposal. All visibility/award/bonus flags must permit issuance.
The whole approval is atomic under the member lock, with an immutable approval
snapshot in a private source receipt and the same stable milestone/ledger keys
as ordinary bonuses. Repeats cannot pay twice; contributions and gross lifetime
earned remain unchanged. This command has not been executed as part of feature
implementation and is never invoked by reconciliation or ordinary awards.

`audit_volunteer_journey` separately reports
`historical_potential_bonus_liability_roo`,
`prospective_pending_bonus_liability_roo` and `unknown_members`. The legacy
`unpaid_bonus_liability_roo` key refers only to prospective pending bonuses.
Unknown members are excluded from calculated liabilities until reconciled.
Potential historical liability is up to 29 Roo through Level 5 and 49 Roo
through Level 6 per eligible member, less any already credited milestones.

Rollback disables new issuance and keeps history/ledger entries. Source receipts
stay retryable; a failed client refresh cannot repeat a wallet transaction.
