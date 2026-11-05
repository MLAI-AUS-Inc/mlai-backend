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

## Current verification

Phase 1 application code is prepared on `codex/one-click-campaign-phase-1`.
The approved migration was created and tested against disposable synthetic
SQLite and PostgreSQL databases. Phase 1 local acceptance has passed.

- 452 database-free CRM tests pass, including the new provider-contract,
  rate/minimum-sample, owner, protected-domain and admission checks.
- Seven deployment-configuration tests pass. Shell syntax, diff whitespace
  and the CI test-assignment audit pass (no newly omitted test modules).
- All 17 new database acceptance tests pass on SQLite and PostgreSQL.
- The wider PostgreSQL run covered 143 cases: 140 initially passed; three
  old contract expectations were updated and their focused rerun passed.
  Those updates cover owner-only mailbox choices, subdomain sender fixtures
  and the new access capability. No production behavior was weakened.
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
