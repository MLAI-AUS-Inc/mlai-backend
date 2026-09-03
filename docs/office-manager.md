# Roo Office Manager contract and runbook

The backend owns the Office Manager of the Day state machine, first-volunteer
selection, coworking booking, Roo Points accounting, and durable Slack repair
state. Public Roo only verifies the Slack interaction, persists it in Roo's
action outbox, and calls the claim endpoint.

## Trust boundary

`POST /api/v1/points/coworking/office-manager/claim/` accepts only the
dedicated `ROO_API_KEY`. Send it as `X-API-Key` or `Authorization: Api-Key`.
The broader `INTERNAL_API_KEY` and `MLAI_API_KEY` credentials are rejected
because the caller chooses the Slack member whose booking and points may be
changed.

The request body is:

```json
{
  "slack_user_id": "U0123456789",
  "date": "2026-09-01",
  "generation": 1,
  "attempt_id": "4482112f-79e1-4ca0-940b-06b24903f796"
}
```

`slack_user_id` must come from Slack's verified action payload, never from the
button value. `date` and `generation` are the Melbourne-local announcement
epoch encoded in the backend's button value. A cancellation increments the
generation, so an unseen or replayed button from the previous announcement
cannot reclaim the reopened day. `attempt_id` is Roo's canonical lowercase UUID for one durable
click and must remain unchanged across transport, response-loss, or restart
retries. The wire representation is strict: `date` must be canonical
`YYYY-MM-DD` and `generation` must be a JSON integer, not a numeric string.
Roo must reject stale dates before calling this endpoint.

## Claim responses

The first accepted claim returns `201` with `status: claimed` and
`replayed: false`. Repeating the exact same `attempt_id`, Slack member, and date
returns that stored result: a replay of the winning attempt is still `201` with
`status: claimed`, now with `replayed: true`. A genuinely new `attempt_id` from
the same winning member returns `200` with `status: already_claimed_by_you`.
Replaying that later attempt returns the same `200` status with
`replayed: true`. All successful responses include the authoritative attempt
ID, date, assignment, booking, points-refund amount, and booking provenance. A
committed attempt is recovered before current-date, feature-flag, or
Slack-profile checks so response-loss retries remain truthful.

Terminal rejections use these codes:

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Missing or malformed Slack ID/date/attempt ID/generation |
| 403 | `member_not_eligible` | Slack member cannot hold the role |
| 404 | `office_manager_day_not_found` | No announcement/day exists |
| 409 | `already_claimed` | Another member won |
| 409 | `claim_closed` | The Melbourne claim window is closed |
| 503 | `feature_disabled` | New claims are disabled |
| 503 | `slack_profile_unavailable` | Slack identity could not be verified yet |

`503` with `code: slack_profile_unavailable`, HTTP `408`, `429`, other `5xx`
responses, transport errors, and malformed success bodies are retryable. Roo
must retry the same attempt ID, Slack member, and date payload from its durable
outbox. It must not substitute the current date, another identity, or a new
attempt ID. Permission, validation, conflict, expiry, and insufficient-balance
responses are terminal.

## Points, cancellation, and Slack messages

The winning member receives a free coworking booking for that date. If an
existing paid booking is converted, the exact 4- or 8-point charge is refunded
idempotently. Cancelling that converted booking atomically reverses the Office
Manager refund before cancellation; if the member no longer has those points,
the cancellation fails without changing the booking or assignment. Standard
cancellation and refund replays are idempotent.

Cancellation mutations must use the immutable `booking_id`. Roo may accept a
date from a member, but it resolves that date to the member's one current
booking before sending the mutation. The backend rejects every date-only
cancellation. This prevents a delayed retry of cancellation N from cancelling
a newer rebooking N+1.

The backend stores the announcement channel and deterministic Slack message
identifiers with the day/assignment. Each outbound delivery is leased with a
per-destination fencing token before Slack is called. A response-loss or
`unknown` result is retried with the same message identity; a replaced worker
cannot overwrite the newer worker's state. Provider I/O happens outside
database row locks and finalization rechecks the lease and current state.
Records that survive a Melbourne-local date
rollover are explicitly marked expired without emitting stale messages.
Mutation lock order is user, date/capacity, Office Manager day, booking, then
assignment. Delivery leases lock only their own assignment row; joined user or
day rows are read after that short transaction so delivery cannot invert the
mutation lock order.

A relinquished winner's public message is retracted from durable state.
Retraction repair runs even when
`OFFICE_MANAGER_ENABLED=false`, so rollback cannot leave a former winner named
publicly. Already-committed winner and reminder deliveries also continue for
the same local date while disabled; the scheduler does not create a new daily
announcement. Unrecoverable retractions remain visible as a health failure on
every scheduler tick until repaired. Public messages may
identify the winner, but private booking and points details belong only in the
winner DM and the claim API response.

## Configuration and rollout

Required backend settings are listed in `.env.example`:

- `ROO_API_KEY`: dedicated caller credential shared only with Public Roo.
- Public Roo's `MLAI_BACKEND_URL` must be the root origin
  (`https://api.mlai.au` in production), with no `/api/v1` path. Roo appends the
  versioned claim path itself, and backend activation rejects a path-prefixed
  companion URL.
- `OFFICE_MANAGER_SLACK_BOT_TOKEN`: Public Roo app bot token. The app that posts
  the button must also receive its interaction callback and have permission to
  read the configured public channel's message history. The backend uses
  `conversations.history` plus deterministic `client_msg_id` values to recover
  accepted Slack posts whose HTTP response was lost.
- `OFFICE_MANAGER_SLACK_CHANNEL_ID`: coworking channel to announce in.
- `SLACK_HTTP_TIMEOUT_SECONDS`: per-request Slack API timeout (default 10
  seconds), preventing a stalled provider call from blocking scheduler health.
- `OFFICE_MANAGER_TIMEZONE`, weekday, announcement, cutoff, and reminder
  settings.
- `OFFICE_MANAGER_ENABLED`: backend creation/claim gate, default off.
- `SCHEDULED_DISCOVERY_POLL_SECONDS` and
`SCHEDULED_DISCOVERY_HEALTH_MAX_AGE_SECONDS`: the scheduler tick and health
freshness bounds. The command exits non-zero when a required Office Manager
delivery reports `false`, terminal failure, or retry exhaustion. The
container removes its success marker and exits rather than hiding the error
in an infinite loop; Compose restarts it, and deployment requires a fresh
successful tick before it can pass.
Every full tick also records start/success/failure timestamps in the shared
database. Production `/healthz/ready` rejects a missing, stale, or more-recently
failed scheduler heartbeat, so web readiness cannot hide a dead scheduler on a
different container filesystem.

If cancellation races an accepted private winner or end-of-day message, the
backend durably locates that deterministic Slack message and replaces its
private booking details with a generic cancellation notice. Public and private
message updates use fenced leases and a bounded retry budget. Permanent
Slack errors and exhausted transient retries become scheduler-visible dead
letters instead of being retried forever.

Production also requires `ROO_API_KEY` and `INTERNAL_API_KEY` to be present,
at least 32 characters, and different. The deploy installs both from separate
secret-store entries. The Slack token, channel, and timezone must remain
configured even while new Office Manager claims are disabled, because durable
updates and retractions still need recovery. Every deploy performs live,
read-only checks of the Public Roo Slack token (`auth.test`), its declared
`channels:history`, `channels:read`, `chat:write`, `im:history`, `im:write`,
`users:read`, and `users:read.email` scopes, the configured public channel
(`conversations.info`, including that the bot is already a member, and
`conversations.history`), and—when new claims are
enabled—Public Roo's non-secret readiness contract. The companion must report the same Melbourne timezone and the exact
backend claim path. It must also report the same non-secret Slack `team_id` and
`bot_id` returned by the backend token's `auth.test`; this proves the app that
posts the buttons is the app whose interactions are routed to Roo. After startup,
`GET /api/v1/points/coworking/office-manager/preflight/` verifies the exact
contract with the Roo credential while internal and missing credentials are
rejected. It does not create a day, booking, or assignment.

### Historical migration identity audit

The Office Manager branch was previously shared under colliding migration
numbers before its append-only `0034`/`0035` identities were established.
Before applying `0034`, `0035`, `0036`, the append-only `0037` provenance
recovery, the append-only `0038` generation hardening, or the append-only
`0039` stale-attempt repair, run the
read-only audit against **every persistent database**, including production,
staging, preview databases with retained volumes, and developer databases:

```bash
python manage.py audit_office_manager_migrations \
  --configured-office-manager-channel "$OFFICE_MANAGER_SLACK_CHANNEL_ID"
```

The audit is read-only. It reads `django_migrations`, database introspection,
and the Office Manager day/assignment rows needed to prove the cross-table
invariants that database constraints cannot express: every claimed day has
exactly one active assignment, and every active assignment belongs to a
claimed day. It reports all recorded Roo identities beginning `0029`, `0030`,
`0031`, `0034`, `0035`, `0036`, `0037`, `0038`, and `0039`, plus the schema markers needed by their
current bodies. In particular, investigate these obsolete shared identities:

- `0029_officemanagerday_coworkingbooking_booking_source_and_more`
- `0030_officemanagerday_coworkingbooking_booking_source_and_more`
- `0031_protect_office_manager_assignment_day`
- `0030_meeting_room_booking`
- `0031_small_and_big_meeting_rooms`

The review lineage is: Office Manager first appeared as `0029` in
`d3596cef3abedd13b71bcfae9f07889a954c2a5d`, was renamed to `0030` in
`f5c6cebd67c2292b597dc8362206b02207b95d96`, and that body was edited in
`593949aa5d5c45888ceb54783e11546002c2e513`; its `0031` protection migration
appeared in `4c60295f5a93de8be4a112510998e9f79789c163`. Meeting rooms first used
`0030` in `bd1e0ab920ef26232e134db63441e07836df876c`, whose body was later edited
in `0ca68a0a2b48d64b46604e7e2de966e99950af18`; meeting-room `0030`/`0031`
were renamed to canonical `0031`/`0032` in
`753caded4dd8f760836e9f3199cdfb0143861944`. Office Manager reached canonical
`0034`/`0035` in `fc60c7412ced9272eef85d41b714961339853e7f`.

An `unsafe` result is a hard stop. Do not edit a shared migration or assume
Django will rerun its changed body. Restore a database clone, compare the body
that database actually ran, and prepare an explicitly reviewed, append-only
schema/data repair. Quiesce every writer—including `web` and `scheduler—for the
whole repair window. Only after the database schema is proven equivalent may
an operator record the canonical replacement identities with Django's
documented fake-migration mechanism. This is a manual maintenance action, not
part of `deploy.sh`.

If obsolete and canonical identities are both recorded and all required
schema markers are complete, the audit returns `attestation_required` and a
`report_sha256`. Pre- and post-migration states have different fingerprints
and therefore require two separately reviewed files outside the repository:

- `/root/mlai-backend-operations/office-manager-migration-pre-attestation.json`
- `/root/mlai-backend-operations/office-manager-migration-post-attestation.json`

Generate the pre-state report against the live database. Generate the expected
post-state report only against a restored clone after applying the exact
reviewed migrations. Do not derive or auto-approve the second fingerprint in
the production deploy. Each file uses this shape:

```json
{
  "version": 1,
  "decision": "reviewed-compatible",
  "report_sha256": "<exact audit fingerprint>",
  "reviewed_by": "<operator identity>",
  "reviewed_at": "2026-09-02T00:00:00+10:00"
}
```

Re-run each state with its corresponding `--attestation-file` and retain both
complete JSON audit outputs with the change record. Each attestation is bound
to one exact recorder/schema fingerprint, so later drift fails closed.
`deploy.sh` mounts the pre file before migrations and the post file after
migrations. It never reuses one attestation across the transition.

`0037` makes unknown historical point-bucket allocations explicit. It
preserves a 0036-era assignment only when its booking provenance and exact
refund ledger agree; otherwise the value becomes null and the audit blocks
activation. An operator must verify the original debit and refund, then record
the exact allocation and durable reviewer evidence with:

```bash
python manage.py reconcile_office_manager_provenance \
  --booking-id <uuid> \
  --purchased-microroo <exact-value> \
  --reviewed-by <operator-identity> \
  --commit
```

If any refund was already reversed, also provide one independently audited
bucket split per reversed assignment:

```bash
  --reversal-purchased-microroo <assignment-id>:<exact-value>
```

Repeat the option for multiple reversals. The command validates the exact
reversal ledger and persists separate immutable reversal-provenance evidence;
it never assumes that the original refund buckets were still present when the
reversal ran.

Run it without `--commit` first. The command never infers a bucket split and
refuses mismatched ledgers or conflicting prior evidence. For a historical
purchased refund, `0038` also records an immutable zero-value adjustment and
reclassifies the exact purchased allocation from the earned bucket back to the
purchased bucket. If that value is no longer safely available, it fails closed
for operator review. Re-run the migration audit after every reconciliation.

Roll out in this order:

1. Run the historical identity audit above for every persistent database.
   Resolve every unsafe or unattested result before continuing.
2. Review and explicitly approve
   `roo.0034_officemanagerday_coworkingbooking_booking_source_and_more` and
   `roo.0035_protect_office_manager_assignment_day`, together with the new
   append-only `roo.0036_office_manager_attempts_and_provenance` and
   `roo.0037_quarantine_legacy_office_manager_provenance` and
   `roo.0038_office_manager_claim_generation` and
   `roo.0039_supersede_reopened_office_manager_attempts` successors.
   Deploy the backend and apply them with `OFFICE_MANAGER_ENABLED=false`.
3. Configure the dedicated Public Roo Slack token and channel, keeping both
   feature flags off. The token must belong to the app that owns the button.
4. Deploy companion Roo PR #210 with `OFFICE_MANAGER_ACTIONS_ENABLED=false`.
5. Smoke-test the exact action ID `office_manager_volunteer_today`, signed
   actor binding, private result delivery, and a duplicate click.
6. Enable the Roo action consumer first. Confirm its readiness contract names
   the expected backend claim URL and `Australia/Melbourne`, then enable the
   backend scheduler. The deployment will validate Slack, the companion
   contract, endpoint authorization, and scheduler health. Monitor claim
   results, pending Slack repairs, scheduler restarts, and container health.

To roll back, disable new Roo actions first and then set
`OFFICE_MANAGER_ENABLED=false`. Do **not** stop the backend scheduler process:
it must drain committed winner-message retractions even while creation and new
claims are disabled. Wait until retraction work is terminal, preserve the
health signal, and investigate any restart loop. Deployment preflight leaves
the last-known-good scheduler running. If a failure occurs after services are
paused but before replacement begins, the deploy restarts the preserved
containers and verifies that the scheduler is still using its prior image and
has produced a fresh successful tick. It stages the backend flag as false in
the host environment for the next replacement; the preserved container keeps
the complete last-known-good environment until then. Once migrations and
post-migration gates succeed and replacement begins, failure recovery may
recreate the new image with the staged-off flag, but it still requires a fresh
scheduler tick. A failure after migration begins but before `migrate --check`
succeeds leaves every writer stopped: Django may have committed only a prefix
of the graph, so neither binary is assumed compatible. Treat the failed deploy
as an operator alert, inspect the recorded migration/schema audit, and repair
forward before restarting services. Do not reverse shared migrations, remove `0034`–`0039`, or
delete Office Manager accounting/provenance rows; roll application code forward
with a new append-only migration when schema recovery is required.
