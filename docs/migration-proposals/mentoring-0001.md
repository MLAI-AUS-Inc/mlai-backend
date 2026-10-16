# Migration proposal: mentoring 0001_initial

Status: PROPOSAL ONLY. No migration file exists and none has been generated,
run or applied. Per AGENTS.md this needs explicit operator approval for this
specific migration before anyone runs `makemigrations`, `migrate`, or any
database-backed test.

Until the migration exists the `mentoring` tables do not exist. Note that
`makemigrations --check --dry-run` does NOT flag this: an app that has no
`migrations` package is not considered by `makemigrations --check`, so CI stays
green and nothing warns you. Do not rely on that check. Until the migration is
applied, the Django admin pages for mentoring return 500 and every user deletion
queries the mentoring tables (the PROTECT foreign keys), so apply the migration
before deploying this code to any database (the staging rollout doc says the
same). The feature flag being off does not avoid either of those.

## Scope

One new app `mentoring`, one new initial migration `mentoring/migrations/0001_initial.py`,
depending on `core` (User) and `roo` (Ledger) latest migrations. Creates 3 tables.
No change to any existing table. No `roo` migration: Ledger rows reuse
source `MANUAL` with reference_type `MENTOR_SESSION`.

## Table mentoring_mentorprofile (model MentorProfile)

| Field | Type | Notes |
| --- | --- | --- |
| id | BigAutoField | primary key |
| user | OneToOne to core.User, CASCADE | related_name mentor_profile |
| is_active | Boolean, default False | set by admin only |
| headline | Char(140), default '' | |
| bio | Text, default '' | |
| topics | JSON, default list | |
| timezone | Char(64), default Australia/Melbourne | |
| away_dates | JSON, default list | list of {from, to} ISO local dates, inclusive; no bookings on those days |
| session_minutes | PositiveSmallInteger, default 30 | validated 15 to 120 in service |
| price_roo | PositiveInteger, default 40 | |
| location_type | Char(16), default video | mlai_space, video, either |
| video_link | URL(500), default '' | mentor's own link |
| space_notes | Char(300), default '' | |
| created_at, updated_at | DateTime, auto | |

Meta ordering: user first_name, last_name, id.

## Table mentoring_mentoravailabilityrule (model MentorAvailabilityRule)

| Field | Type | Notes |
| --- | --- | --- |
| id | BigAutoField | |
| mentor | FK MentorProfile, CASCADE | related_name availability_rules |
| weekday | PositiveSmallInteger | 0 Monday to 6 Sunday |
| start_time, end_time | Time | mentor local time |

Constraints: CHECK weekday <= 6 (mentor_rule_weekday_range);
CHECK end_time > start_time (mentor_rule_end_after_start).
Ordering: weekday, start_time.

## Table mentoring_mentorsession (model MentorSession)

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID primary key, default uuid4 | |
| mentor | FK MentorProfile, PROTECT | related_name sessions |
| founder | FK core.User, PROTECT | related_name mentor_sessions |
| status | Char(12), default requested | requested, accepted, declined, cancelled, expired, completed |
| starts_at, ends_at | DateTime | |
| price_roo | PositiveInteger | price snapshot |
| purchased_points_cost_microroo | BigInteger, default 0 | exact refund split |
| location_type | Char(16) | mlai_space or video |
| location_detail | Char(500), default '' | video link or space notes snapshot |
| brief_stage | Char(200) | |
| brief_struggle | Char(1000) | |
| brief_outcome | Char(1000) | |
| brief_link | URL(500), default '' | optional |
| client_request_id | UUID | idempotency |
| hold_ledger_entry | FK roo.Ledger, null, PROTECT, related_name '+' | spend row |
| release_ledger_entry | FK roo.Ledger, null, PROTECT, related_name '+' | refund row |
| payout_ledger_entry | FK roo.Ledger, null, PROTECT, related_name '+' | mentor award row |
| requested_at, expires_at | DateTime | |
| responded_at, cancelled_at, completed_at | DateTime, null | |
| cancelled_by | FK core.User, null, SET_NULL, related_name '+' | |
| notification_status | Char(8), default pending | pending, sent, failed, skipped; separate from status |
| notification_attempted_at | DateTime, null | |
| created_at, updated_at | DateTime, auto | |

Constraints: CHECK ends_at > starts_at (mentor_session_end_after_start);
UNIQUE (founder, client_request_id) (mentor_session_unique_request).
Indexes: (mentor, status, starts_at) as mentor_sess_mentor_idx;
(founder, status, starts_at) as mentor_sess_founder_idx.
Ordering: starts_at, id.

There are no rating, strike, testimonial or report columns by design.

## Risk and rollout notes

- Purely additive: three new tables, no data backfill, no change to existing rows.
  Reverse migration drops the three tables.
- Deploy gate: deploy.sh compares the sha256 of `migrate --plan` with
  `APPROVED_MIGRATION_PLAN_SHA256` on the host. The operator must set it for
  the plan containing `mentoring.0001_initial` before the main deploy.
- Repo paperwork the approver may also want: a docs/*migration-inventory*.json
  entry (see tests/test_refactor_migration_inventory.py) once the real file
  exists and its sha256 is known.
- DB-backed tests (integration of locking, constraints, ledger rows) are not
  written or run yet; they also need this approval.
- The feature flag MENTOR_BOOKING_ENABLED is off by default, so applying the
  migration changes no behavior.

## Open items (not changed yet, need a decision before the migration is generated)

- PROTECT vs SET_NULL on user deletion: `MentorSession.mentor` and `.founder` are PROTECT,
  so deleting a user who has any session raises ProtectedError (account deletion and
  admin delete). Decide the account-deletion behavior (anonymise, SET_NULL with nullable
  fields, or block with a clear message) and confirm the account deletion path.
- Optional exclusion constraint against overlapping active sessions per mentor. It
  needs the `btree_gist` extension and an ExclusionConstraint on (mentor, tstzrange of
  starts_at and ends_at) for statuses requested and accepted. Today only the Postgres
  advisory locks and the in-transaction slot recheck protect this.
