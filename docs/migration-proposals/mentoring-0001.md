# Migration: mentoring 0001_initial

Status: CHANGED, AWAITING OPERATOR APPROVAL (16-10-26). The Google Meet and free/busy work
changes the field list below. The existing file `mentoring/migrations/0001_initial.py` is left
in place unchanged and is **to be regenerated after approval** (it still has `video_link` and
lacks the new fields, so `makemigrations --check` reports changes until then). Do not apply it.
Nothing has been applied anywhere.

Field changes against the previously approved file (the tables below show the new list):

- mentoring_mentorprofile: REMOVE `video_link`; ADD `busy_calendar_email` Email(254), blank, default ''.
- mentoring_mentorsession: ADD `meet_link` URL(500), blank, default ''; `calendar_event_id`
  Char(255), blank, default ''; `meeting_status` Char(12), default pending (pending, created,
  failed, unavailable); `meeting_error` Char(40), blank, default ''; `meeting_path` Char(16),
  blank, default '' (meet_api or calendar).
- No new tables, constraints or indexes. Still a single initial migration (nothing is
  applied, so no 0002 is needed); the regenerated file would also change the sha256 and the
  `APPROVED_MIGRATION_PLAN_SHA256` text below stays valid only if the plan text is unchanged
  (it is, because the operation list is the same).

Previous status: APPROVED by the operator on 16-10-26 for this private copy only. The file
`mentoring/migrations/0001_initial.py` was generated with
`makemigrations mentoring` under dummy no-database settings. It has NOT been applied
to any database (no `migrate`, no database-backed test). This document describes the
generated file exactly.

- File sha256: `2e808c71547fc854d09cc1b194669f818dbefe203906f44c6d8434e1a633c635`
- Dependencies: `roo.0041_apple_iap_digital_credits` (Ledger) and the swappable user
  model (`core.User`).
- Checks: `makemigrations --check --dry-run` reports "No changes detected" and
  `manage.py check` is clean (run via the no-database harness,
  `scripts/test_without_database.py --check-models`).
- Purely additive: three new tables, no change to any existing table, no backfill.
  Reverse = drop the three tables.

## Table mentoring_mentorprofile (MentorProfile)

| Field | Type |
| --- | --- |
| id | BigAutoField, primary key |
| user | OneToOne to core.User, CASCADE, related_name mentor_profile |
| is_active | Boolean, default False (admin only) |
| headline | Char(140), blank, default '' |
| bio | Text, blank, default '' |
| topics | JSON, blank, default list |
| timezone | Char(64), default Australia/Melbourne |
| away_dates | JSON, blank, default list: {from, to} ISO local dates, inclusive |
| session_minutes | PositiveSmallInteger, default 30 |
| price_roo | PositiveInteger, default 40 |
| location_type | Char(16), choices mlai_space or video, default video |
| busy_calendar_email | Email(254), blank, default '' (calendar shared free/busy with the MLAI account) |
| space_notes | Char(300), blank, default '' |
| created_at, updated_at | DateTime, auto |

Ordering: user first_name, last_name, pk.

## Table mentoring_mentoravailabilityrule (MentorAvailabilityRule)

| Field | Type |
| --- | --- |
| id | BigAutoField |
| mentor | FK MentorProfile, CASCADE, related_name availability_rules |
| weekday | PositiveSmallInteger (0 Monday to 6 Sunday) |
| start_time, end_time | Time (mentor local) |

Constraints: CHECK weekday <= 6 (`mentor_rule_weekday_range`);
CHECK end_time > start_time (`mentor_rule_end_after_start`). Ordering: weekday, start_time.

## Table mentoring_mentorsession (MentorSession)

| Field | Type |
| --- | --- |
| id | UUID primary key, default uuid4 |
| mentor | FK MentorProfile, CASCADE, related_name sessions |
| founder | FK core.User, CASCADE, related_name mentor_sessions |
| status | Char(12), default requested (requested, accepted, declined, cancelled, expired, completed) |
| starts_at, ends_at | DateTime |
| price_roo | PositiveInteger (price snapshot) |
| purchased_points_cost_microroo | BigInteger, default 0 (set when charged, for an exact refund split) |
| location_type | Char(16), mlai_space or video |
| location_detail | Char(500), blank, default '' |
| brief_stage | Char(200) |
| brief_struggle | Char(1000) |
| brief_outcome | Char(1000) |
| brief_link | URL(500), blank, default '' |
| client_request_id | UUID |
| requested_at, expires_at | DateTime |
| responded_at, cancelled_at, completed_at | DateTime, null |
| notification_status | Char(8), default pending (pending, sent, failed, skipped) |
| notification_attempted_at | DateTime, null |
| meet_link | URL(500), blank, default '' (Google Meet link, made on accept) |
| calendar_event_id | Char(255), blank, default '' |
| meeting_status | Char(12), default pending (pending, created, failed, unavailable) |
| meeting_error | Char(40), blank, default '' (short code) |
| meeting_path | Char(16), blank, default '' (meet_api or calendar) |
| created_at, updated_at | DateTime, auto |
| cancelled_by | FK core.User, null, SET_NULL, related_name '+' |
| charge_ledger_entry | FK roo.Ledger, null, SET_NULL, related_name '+' (spend row, written on accept) |
| release_ledger_entry | FK roo.Ledger, null, SET_NULL, related_name '+' (refund row) |
| payout_ledger_entry | FK roo.Ledger, null, SET_NULL, related_name '+' (mentor award row) |

Constraints: CHECK ends_at > starts_at (`mentor_session_end_after_start`);
UNIQUE (founder, client_request_id) (`mentor_session_unique_request`).
Indexes: (mentor, status, starts_at) `mentor_sess_mentor_idx`;
(founder, status, starts_at) `mentor_sess_founder_idx`. Ordering: starts_at, pk.

There are no rating, strike, testimonial or report columns by design.

## Decisions taken (resolved open items)

- User deletion: the mentor profile (and so its sessions) and the founder foreign key use
  CASCADE, so deleting a user is never blocked. The three ledger foreign keys are SET_NULL,
  so ledger rows are never touched by, and never block, a session delete. Ledger rows stay.
  A charged, uncompleted session removed by a cascade is not refunded automatically.
- No exclusion constraint against overlapping active sessions. Overlap protection relies
  on Postgres advisory locks and an in-transaction slot recheck. Future option: an
  `ExclusionConstraint` on (mentor, tstzrange of starts_at and ends_at) for statuses
  requested and accepted, which needs the `btree_gist` extension and a separate approval.
- Note: `makemigrations --check` does not flag an app that has no `migrations` package. That
  is why this file must be committed together with the code; without it the tables do not
  exist, admin pages 500 and user deletion queries missing tables.

## APPROVED_MIGRATION_PLAN_SHA256

`deploy.sh` hashes the `migrate --plan` output, cut to start at the line
`Planned operations:` by `scripts/validate_website_deploy_config.py --migration-plan`
(trailing newlines removed). `migrate --plan` reads applied state from the database, so the
true value depends on what is pending on that host. Computed without any database,
assuming `mentoring.0001_initial` is the ONLY pending migration (as on a host already
up to date with this branch's base), the plan text is:

```
Planned operations:
mentoring.0001_initial
    Create model MentorProfile
    Create model MentorAvailabilityRule
    Create model MentorSession
```

and its sha256 is `35394050fd862c66860acd992b562efd24f8793f7938dd021ed2c7db09b94393`.

If anything else is pending on the host (on staging the 98 commits it is behind main may add
migrations), the value differs. The staging or production operator must run, on that host
after pulling the branch and before deploying:

```
python manage.py migrate --plan --noinput | python3 scripts/validate_website_deploy_config.py --migration-plan | sha256sum
```

check that the plan lists only intended migrations, and set that value as
`APPROVED_MIGRATION_PLAN_SHA256` in the host env file.
