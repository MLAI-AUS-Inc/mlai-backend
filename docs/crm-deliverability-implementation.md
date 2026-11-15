# One-click campaign delivery implementation

Source plan: `one-click-campaign-sending.md`, supplied 5 November 2026.
Audited main: backend `93e80a1`, Chat `4f6dca2`. This is an implementation
record, not evidence that production configuration is ready.

## Existing behavior

- CRM already stores contacts, consent evidence, suppressions, audiences,
  events, drafts, deliveries, replies and audits. Community opt-ins recheck
  the original account's current consent.
- `blasts.py` snapshots recipients with count/fingerprint confirmation and
  dispatches email through listmonk. Resource IDs and delivery uniqueness
  protect interrupted ticks from duplicate sends.
- Signed Resend feedback currently accepts only exact configured senders,
  uses audit rows as receipts, and disables both monitored mailboxes after
  any non-simulator failure. It treats every bounce as permanent.
- Sending domains and mailbox enablement already have DNS checks.
- Chat has a multi-channel composer, audience builder, tests, review,
  recipients and replies. It currently exposes mailbox selection and daily
  limits rather than a stream plan.
- There are no stream, wave, provider-event, incident, seed, reputation or
  day-budget models on main. The capped sender-pool PR #1271 remains open;
  it is not part of this base or a substitute for the proposed stream ramp.

## Phase 1 schema (creation and disposable tests approved)

Migration: `crm.0002_deliverability_foundations`, dependent on
`crm.0001_initial` and the swappable user model. The exact field definitions
are reviewable in `crm/models.py` on this branch. Operations:

1. Create `SendingStream`: unique key/domain, protected mailbox FK, purposes,
   transactional flag, state/reason/detail/timestamp, ramp stage/timestamp,
   nullable budget override/last-send day, peak volume, policy overrides.
2. Create `EmailEvent`: source/type, indexed provider ID, unique SHA-256
   receipt, recipient/sender/provider, nullable stream/blast/delivery FKs,
   bounce type/subtype, SMTP/enhanced codes, masked diagnostic, occurrence
   and receipt times, stream/type/receipt-time index.
3. Create `DeliverabilityIncident`: stream/blast/provider scope,
   signal/severity/value/threshold/sample/action, lifecycle and owner
   acknowledgement fields, notes, detection/alert/action timestamps.
   A conditional unique fingerprint prevents duplicate open incidents;
   an index supports the open incident list.
4. Add nullable `Blast.stream`, plus pause state/reason/actor/time.
5. Add `Contact.soft_bounce_streak` (zero by default).
6. Add `BlastDelivery.bounce_kind` (empty by default).

No deletion, consent rewrite, provider send, mailbox enablement or production
operation is included. No historical blast is reassigned in this phase.
Wave-zero adoption belongs to Phase 2. New identities are created explicitly
by the idempotent `crm_streams sync` command, disabled, after schema adoption;
the schema migration itself performs no data backfill.

Disposable test scope: the unchanged 381-migration inventory in
`crm-deliverability-phase-1-existing-migrations.json`, plus only the proposed
`crm.0002_deliverability_foundations`. Existing inventory SHA-256:
`185a1da6e420dc88270219aaceb9251da7924981a9ae6d4477cf675cc4b59aa3`.
The user approved creation and disposable local/CI application of this exact
scope on 5 November 2026. Migration file SHA-256:
`872fc652ee6b5f4fc3a6072ddc952d31a7f98fb7325eb00256505a37e984db9f`.
This approval does not grant production migration, deployment or live sending permission.

## Phase gates

| Phase | Scope | Gate |
| --- | --- | --- |
| 0 | Provider/DNS/SMTP/seed/owner setup | Required before production rollout; not yet verified |
| 1 | Streams, protected domains, event receipts, rate-based guardian | Local acceptance passed; production setup remains gated |
| 2 | Planner, waves, volume, pause/resume, legacy adoption | Phase 1 acceptance passes first |
| 3 | Bound preflight, link checks, rspamd and IMAP seeds | Phase 2 acceptance, then real staging seed evidence |
| 4 | Staff composer and progress UI | Phase 3 acceptance, mock-bridge tests and `just ci` |
| 5 | Full monitoring, alerts and owner health UI | All simulation scenarios and real staging data |
| 6 | Activity lifecycle and reconfirmation | Existing pilot ends before warm-up retirement (17 November) |
| 7 | Transactional identity and authentication hardening | Verified legitimate senders before DMARC changes |

## Phase 5 implementation and remaining gates

The monitoring branch adds exact-evidence recovery, provider and stream scopes,
bounded Postmaster/DNS/reputation/config/suppression checks, enrolled-seed daily
heartbeats, dedicated notify owner alerts, and aggregate health/policy APIs.
It reuses the approved schema: no fifth migration is created. All new external
monitoring/sending switches default false. Secrets are wired as secrets in the
deployment source and are not assigned to any connected environment.

The Chat health branch adds owner-only stream/provider graphs, check coverage,
seed history, explicit check actions, an incident decision dialog and audited
policy controls. Phase 4 remains separately reviewable in Chat PR #654 and
backend PR #1295. Neither branch has been merged or deployed.

Primary provider contracts were verified during implementation. Important plan
corrections: Postmaster's scope is `postmaster.traffic.readonly`; Resend has no
documented read-only key permission; Safe Browsing is noncommercial; seven seed
mailboxes across two streams can generate fourteen daily test messages.
Operational setup and real staging evidence are still required. The simulation
command qualification and Phase 6/7 work remain tracked under the original implementation goal.

The frozen monitoring source passed all **799 CRM tests** on the approved
384-migration disposable PostgreSQL graph, including 16 monitor database tests
and every admin route. The 91 focused database-free guard/monitor/preflight
checks and eight deployment-configuration checks passed. CI assignment reports
zero new omissions. Shell syntax and diff whitespace checks passed. External
network access was denied throughout synthetic backend qualification.

## Simulation command

`crm_simulate_send --scenario <name> --recipients 5000 --days 8` drives the real
planner, slot allocator, dispatcher, guardian, recovery and day-close code with
a fixed Melbourne clock and fake listmonk adapter. Fault scenarios inject
synthetic provider events and monitor observations. Every run rolls back.

The command refuses unless `APP_ENV=test`, the PostgreSQL database name starts
with `test_`, `CRM_SIMULATION_DATABASE` matches that exact database name, and
the CRM contains no contacts, streams, seed mailboxes, campaigns or events.
It never creates a database, applies a migration, reads an inbox or contacts a
provider. HTTP and new socket connections are denied for the entire fixture
transaction. No existing database is inferred to be disposable from its URL.

All twelve plan scenarios are implemented: healthy six-day ramp, canary bounce,
complaint recovery, Microsoft deferral, Google policy rejection, feedback
outage, DNS drift, listmonk outage, late Postmaster spam, staff pause/resume,
event priority, and idle decay. Reports contain aggregate handoffs, incident
states and invariant outcomes, without recipient addresses. The command checks
both daily and hourly ceilings, one initial start per provider campaign, and
one delivery snapshot per person. A staff resume uses the same campaign.
Time-critical events take priority at the next available slot; an already
handed-off batch retains its original resource and capacity accounting.

Qualification passed: **813 CRM tests**, including all twelve 5,000-recipient
scenarios and the command's refusal/rollback checks, on the approved disposable
PostgreSQL graph (321.460 seconds). The lock was regenerated from both inputs;
all pre-existing package pins were retained. The 91 focused pure checks and 30
simulation/monitor database checks also passed with the newly hash-locked
monitor dependencies. This is synthetic evidence, not a live send or staging
rehearsal. No migration or connected environment was changed.

## Phase 6 lifecycle implementation

The lifecycle branch reuses the approved `last_active_at`,
`consent_reconfirmed_at`, domain cache and JSON metadata; no new schema is
required. `CRM_LIFECYCLE_ENABLED` defaults false. Its deployment settings are
source wiring only, with no connected environment changed.

- A resumable daily batch takes the latest valid login, attendance,
  registration, click, reply and dated Substack click. It ignores opens,
  undated aggregates and future timestamps. Contact locks preserve concurrent
  preference changes. A verified action lifts only the dormant flag.
- Owners prepare a fixed-audience reconfirmation draft on an active stream.
  Normal review/preflight/queue checks still apply. Only tier D, affirmative
  permission within 36 calendar months, and no historical hard bounce qualify.
  Checks use at most 10% of the shared daily stream budget and rank behind
  ordinary news. The existing A/B/C delivery tier remains blank for these
  checks; the plan reports D explicitly, avoiding a schema change.
- Per-person signed links bind the current address, permission and delivery.
  GET is read-only; CSRF-protected POST records explicit reconfirmation and
  activity without changing the original consent date or any other hold.
  Expiry, withdrawal, ban, address change or hard-bounce history refuses the
  link. Test/seed buttons use a read-only preview URL. Thirty days without
  an action adds `dormant_suppressed`, retaining the contact.
- Cohorts with at least five negative outcomes and three times the overall
  rate place only untouched people on review hold. Mature provider click
  comparisons need 500 handoffs and at least three observed baseline days;
  missing baselines remain unknown. Neither uses opens.
- Import reports distinguish no-MX, disposable, likely typo and unknown.
  At most one worker batch of new domains is inspected per chunk. A bounded
  background pass advances past unknown answers and fills the remaining
  cache. New imported contacts retain the first import's internal ID.
- Known typo corrections require a human confirmation. They preserve all
  consent/channel/delivery states and retain a protected original-address
  alias. Luma/CSV reimports and late opt-outs resolve that alias; current
  and future bans also follow the linked user and aliases. A committed audit
  version prevents a stale alias cache from recreating the old identity.
  Conflicts are refused rather than merged. This path requires PostgreSQL.

New owner endpoint: `POST deliverability/reconfirmation/` with `{stream}`.
Admin contact endpoint: `POST contacts/{id}/correct-email/` with
`{expected, replacement}`. The public signed preference action is
`/api/v1/crm/preferences/reconfirm/`. Tokens are redacted by request logging;
responses are non-cacheable and disclose no recipient address.

Optional DMARC XML ingestion and LLM copy suggestions remain deferred. The
finite warm-up run is not retired before its planned 17 November end; verify
its final state before a separate cleanup. No pilot run, audience permission
or sender configuration was altered by this implementation.

## Current verification

Phase 1 application code is prepared on `codex/one-click-campaign-phase-1`.
The approved migration was created and tested against disposable synthetic
SQLite and PostgreSQL databases. Phase 1 local acceptance has passed.

- 452 database-free CRM tests pass, including the new provider-contract,
  rate/minimum-sample, owner, protected-domain and admission checks.
- Seven deployment-configuration tests pass. Shell syntax, diff whitespace
  and the CI test-assignment audit pass (no newly omitted test modules).
- All 17 new database acceptance tests pass on SQLite and PostgreSQL.
- The complete frozen Phase 1 source passed all 650 CRM tests on PostgreSQL.
  The updated contract expectations cover owner-only mailbox choices,
  subdomain sender fixtures and the new access capability.
- Django model and migration-drift checks pass with no pending model changes.
- No production schema/configuration, pilot, provider or live audience was
  changed. No deployment workflow or live send was invoked.

The new guardian remains off by default. This phase prepares incident and
recovery state; the effective gradual budget, canary and recovery pacing are
Phase 2 acceptance work. The staff UI, preflight, complete monitors and
transactional migration remain later phases of the supplied plan.

Owner recovery commits its decision and a pending-resume marker before any
provider restart. The dispatcher rechecks admission and observes the existing
campaign before resuming it. A timeout or lost database acknowledgement can
be retried without creating a campaign or restarting one already running.
Stopping the blast or a new guardian hold blocks that pending recovery.
Acknowledged incidents are rechecked under lock before sending reminders.

## Phase 2: waves and planning

Prepared on `codex/one-click-campaign-phase-2`, stacked on Phase 1 PR #1288.
The user approved creation and disposable testing of the exact
[0003 schema and adoption](crm-deliverability-phase-2-schema.md).
No production migration or live send is included.

- Consent-preserving domain hygiene and activity tiers produce a stable,
  provider-interleaved snapshot. The disposable-domain dataset is pinned to
  upstream CC0 revision `a72c0cecfcdd940d666265e44c0f661282e2d4aa`, with its
  license and source checksum bundled. Unknown MX is a separate pending
  check, never a false `no_mx` or a contact edit.
- Stream locks reserve daily/hourly capacity before provider calls. Canary,
  sending windows, event priority, provider caps, frequency caps and idle
  decay use the same central policy as estimates. New campaigns can span
  days without being rejected by the old per-mailbox campaign-size limit.
- Each wave owns one deterministic list/campaign. A lost start response is
  reconciled against that resource even after the slot changes. Positively
  unstarted drafts may expire and release rows with their original ranks.
- Final consent checks remove newly excluded people from this list only.
  Staff controls commit intent before external effects; system recovery
  remains owner-only. If listmonk refuses cancellation of a paused campaign,
  it stays paused permanently and is locally cancelled; the backend never
  resumes it merely to cancel it. Sync continues checking that retained pause.
- Feedback maps to the correct wave even when its snapshot is over a week
  old. Per-wave click URLs, bounce receipts, day totals and aggregate blast
  outcomes are reconciled. Day close promotes only once, using actual volume
  and known healthy feedback; missing feedback cannot promote.
- The planner endpoint is read-only, and detail responses add plan, waves and
  results. The finite warm-up keeps its existing dispatch path and limits.

Acceptance covers a real dispatcher simulation of 5,000 synthetic recipients:
150 / 300 / 600 / 1,000 / 1,500 / 1,450 across six sending days, skipping the
weekend, obeying hourly caps and the 45-minute canary gate, with one provider
start per wave. Further tests cover concurrent dispatchers, legacy completion,
migration reversal/re-adoption, lost responses, consent races, frequency
deferral, event priority, provider isolation, pause/resume/cancel, and stale
or missing feedback. All provider effects are mocked.

Two implementation clarifications are recorded explicitly:

1. A new 150/day stream's minimum 50-person canary needs a bounded initial
   allowance above the nominal 33-person hourly cap. Later slots use the
   ordinary formula; the source plan's 100-person second-hour burst is not
   permitted.
2. A fully accounted-for batch can be followed by a quiet interval while
   waiting for capacity. Unanswered sends still require recent feedback.
   Otherwise a successfully delivered 50-person canary would deadlock
   before its next permitted slot.

`CRM_WAVES_ENABLED` remains false by default. Bound preflight, real inbox
acceptance, staff UI, full monitors and lifecycle/transactional changes are
later phases, not claims of production readiness.

Final Phase 2 verification: all **694 CRM tests** passed on a frozen archive
of commit `61ba0ce` in disposable PostgreSQL (127.6 seconds). The complete
approved 383-migration graph was checked by exact file hashes. Seven deploy
configuration tests, model/migration drift, workflow YAML parsing, shell
syntax, diff whitespace and the CI assignment audit also passed. No
production configuration, provider resources or recipient data were used.

## Phase 3: bound checks and dedicated inbox probes

The approved [0004 schema](crm-deliverability-phase-3-schema.md) was created
without changing any existing migration. The preflight routes and automatic
fixes, spam-check MIME client, internal Rspamd deployment files and read-only
IMAP adapter are implemented with flags off. Every content/audience/system
check has a synthetic fixture. The check binds more than the source plan's
minimum: it also includes actual template bytes, the selected From/Reply-To
and event substitutions, preventing changed provider templates or event data
from reusing an old check. Send refreshes critical system configuration before
the database transaction and validates the current hash inside it.

Probes have their own empty resources, snapshot, token and dedicated inbox
recipients. Submission is recorded before listmonk's non-idempotent test call;
unknown results are polled, never resubmitted blindly. Tests prove that edits
cannot alter the old probe or authorize new content, and that contact consent
and status remain unchanged. A timeout halves the first canary only and records
an amber incident. Owner fields do not reach general staff responses.

Remaining release evidence is explicit: no Rspamd container has been started,
no actual inbox has been accessed, no real seed has been sent and no provider
or production configuration has changed. The selected immutable image must
pass its own configuration/scoring checks; a separately authorized staging
rehearsal must prove receiver authentication and real signed unsubscribe
headers. OAuth seed access tokens are supplied by host secrets and must be
refreshed there; this adapter does not claim to manage OAuth grants.

Phase 3 final local verification: **748 CRM tests** passed on the frozen
`9105d3d` source in disposable PostgreSQL with the approved 384 migration hashes
checked. [Draft PR #1293](https://github.com/MLAI-AUS-Inc/mlai-backend/pull/1293)
is stacked on Phase 2; no release action is implied.

## Phase 4 API support

The staff UI's presets, current source-permission predicate, masked preview
list and full plain status catalogue are prepared on
`codex/one-click-campaign-phase-4-contract`. This phase adds no schema.
Access explicitly advertises the waves/preflight flags. Blasts return pause
and status copy in list responses as well as detail. No contact permission is
changed by these staff-composition operations.

## Provider contract checked during implementation

Resend's current event references document `bounce.type`, `bounce.subType`
and `bounce.message`; failure diagnostics are `failed.reason`, and
suppression diagnostics are `suppressed.type` / `suppressed.message`.
The delayed-event example provides no SMTP diagnostic. Never invent a
provider policy code from a delay alone. Tags are documented, arbitrary
custom headers in feedback are not assumed.

- <https://resend.com/docs/webhooks/emails/bounced>
- <https://resend.com/docs/webhooks/emails/delivery-delayed>
- <https://resend.com/docs/webhooks/emails/failed>
- <https://resend.com/docs/webhooks/emails/suppressed>

## Phase 0 public DNS observation

The read-only [public DNS snapshot](crm-deliverability-public-dns-2026-11-05.json)
found no Resend DKIM or return-path SPF/MX records for either planned stream
domain. The root DMARC record is present; explicit stream DMARC records are
absent. This checks only public DNS, not provider verification, key scope,
SMTP configuration, quotas, Postmaster access or seed accounts. Production
readiness remains unverified and Phase 0 setup is still required.

## Phase 6 qualification and Phase 7 source completion

The frozen lifecycle source passed all 838 CRM tests in disposable PostgreSQL
(351.978 seconds), validating the same approved 384 migration hashes. It adds
no migration. Policy/configuration, model-drift, shell and test-assignment
checks passed. The lifecycle UI uses explicit correction confirmation and
owner-prepared reconfirmation drafts; the focused browser regression passed
31 campaign, contact, event and Health scenarios.

Phase 7 adds the canonical system sender to all five named transports,
Customer.io From and tracking controls, dedicated notify-key preference and
an independent default-off system-mail monitor. Start/result metadata is
payload-free and savepoint-isolated. Provider and purpose failure rates,
Resend hard bounces and sustained delivery p95 can only alert. Missing
Customer.io delivery feedback and unverified DKIM remain unknown. The
[runbook](crm.md#system-email-and-sign-in-monitoring-phase-7) records the
provider cutover, staging-message and staged DMARC gates. No connected
environment, DNS, provider, schema or campaign was changed by this source work.

The optional DMARC XML reader and LLM features remain deferred. Retiring the
finite warm-up requires its actual completed run after 17 November. Source
qualification is separate from staging rehearsal, production rollout and
authorization to send.

Phase 7 qualification: **943 tests passed** on frozen source in disposable
PostgreSQL (230.372 seconds), covering all CRM suites, sign-in delivery,
approval notifications, founder outboxes and notification channels. The
86 focused pure checks, model drift, configuration, shell syntax and test
assignment checks passed (848 fully selected modules, no new omissions). A
subsequent notification response-shape adjustment is covered by the seven
transactional pure tests plus eight deployment-configuration checks. The
approved migration graph remains exactly 384 unchanged hashes. No live
provider call, inbox access, DNS update, deployment or send was performed.

Final source qualification, after the response-shape and staff-activity
checks: **945 tests passed** on the frozen combined PostgreSQL selection
(287.257 seconds). All frozen source hashes matched the working source
before this documentation update. System-mail receipts are excluded from
the Contacts activity feed, and the monitor uses the existing action/time
audit index. The seven transactional pure tests and eight deployment
configuration tests also pass. The schema remains the approved graph only.

## Campaign domains on mlai.org.au (approved 13 Nov 2026)

Campaign mail moves off mlai.au entirely. mlai.au keeps staff mail (Google
Workspace) and system mail (`notify.mlai.au`). Campaigns and event updates send
from two subdomains of a second registered domain, so a bad campaign cannot
touch mlai.au reputation and a slowed marketing stream cannot hold event
updates. This is configuration only; `crm/tests/test_org_streams_unit.py`
proves the code accepts it.

| GitHub variable / droplet `.env` | Value |
| --- | --- |
| `CRM_STREAMS` | `news:news.mlai.org.au:hello@news.mlai.org.au:marketing;events:events.mlai.org.au:events@events.mlai.org.au:event_update;notify:notify.mlai.au:notifications@notify.mlai.au:transactional` |
| `CRM_SENDING_DOMAINS` | `news.mlai.org.au:resend,events.mlai.org.au:resend` |
| `CRM_PROTECTED_DOMAINS` | `mlai.au,mlai.org.au` |
| `CRM_LISTMONK_PUBLIC_URL` | `https://links.mlai.org.au` (listmonk `app.root_url` must match; keep `lists.mlai.au` serving old unsubscribe links) |
| `CRM_EVENT_SEND_WINDOW` | `mon-sun 08:00-20:00 Australia/Melbourne` (evening "starts soon" reminders for events within `CRM_TIME_CRITICAL_HOURS`) |

Order: Resend domains verified and listmonk SMTP routes in place first, then the
variables, then `python manage.py crm_streams sync` (mailboxes are created
disabled; an owner enables each in Admin → Sending), then monitors and guardian,
then waves and preflight together with the mlai-chat staff screens and
`CRM_LEGACY_MAILBOX_PATH_ENABLED=false`.
