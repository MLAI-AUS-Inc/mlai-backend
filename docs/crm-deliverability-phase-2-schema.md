# Phase 2: campaign waves and audience planning schema

Prepared after Phase 1 local acceptance: all 650 CRM tests passed on a
disposable PostgreSQL instance at `34028d2`; Phase 1 review is PR #1288.

## Exact proposed migration

`crm.0003_campaign_waves_and_planner`, depending only on
`crm.0002_deliverability_foundations`. Field definitions and constraints are
reviewable in `crm/models.py` on `codex/one-click-campaign-phase-2`.

Schema operations:

1. Create `BlastWave`: UUID/timestamps; blast FK and unique per-blast index;
   canary/regular kind; loading/running/paused/finished/cancelled/failed state;
   size; indexed `reserved_at`; nullable list/campaign IDs, start/finish times;
   stats JSON. Campaign ID is unique when present. A conditional constraint
   permits only one loading/running/paused wave per blast.
2. Create `StreamDay`: stream FK and Melbourne date (unique together), budget,
   ramp stage, sent/delivered/hard-bounce/soft-bounce/deferral/complaint/
   unsubscribe/open/click counts, provider and Postmaster JSON, nullable
   healthy result.
3. Create `MailDomainInfo`: unique domain, provider, MX JSON, nullable has-MX
   result, disposable flag and checked time. Unanswered checks stay unknown.
4. Add `Blast.plan` JSON.
5. Add nullable `BlastDelivery.wave`, `send_rank`, tier and provider; add
   status `cancelled` (not sent), and index `(blast, status, send_rank)`.
6. Add nullable wave FKs to `EmailEvent` and `DeliverabilityIncident`.
7. Add nullable `Contact.last_active_at` and indexed `Contact.email_domain`.

Data operations in the same migration:

- Fill `email_domain` by lowercasing the existing email's domain; no address,
  consent, suppression or activity history is rewritten.
- Every queued/sending email blast receives wave zero containing its current
  list/campaign IDs and email delivery rows. Existing provider ownership,
  delivery status and recipient identities are preserved. The wave records
  its legacy-adoption marker and known campaign state/start time; unknown
  provider state remains unstarted until reconciliation, not a fresh send.
- Assign that wave to existing feedback events only through their known
  delivery FK. Unmatched feedback and blast-level incidents remain unscoped.
- Keep existing legacy IDs on the blast and record adoption metadata. Do not
  enable mailboxes, create provider resources, change stream state, or send.
- Test reversal removes only the new adoption metadata before removing the
  new schema; preserved legacy IDs and delivery statuses allow re-adoption.

## Disposable test scope

The unchanged 382 existing migrations are exactly the 381 entries in
`crm-deliverability-phase-1-existing-migrations.json` (inventory SHA-256
`185a1da6e420dc88270219aaceb9251da7924981a9ae6d4477cf675cc4b59aa3`), plus
`crm.0002_deliverability_foundations` (file SHA-256
`872fc652ee6b5f4fc3a6072ddc952d31a7f98fb7325eb00256505a37e984db9f`).

The user approved creation and disposable testing of this exact scope on
5 November 2026. Created file SHA-256:
`816786a2fe343bee10ab13a0b9a69d0978c6b9cf64ba7cc7c10a458b26fb3b49`.
The approval covers only the proposed 0003 migration and application of the
above graph only to disposable local/CI databases with synthetic data,
including reversing/reapplying 0003 to test legacy adoption. No production
or connected staging migration, deployment or live email is included.

## Volume interpretation

The source plan's first-hour formula gives a new 150/day stream a nominal
hourly cap of 33, below its minimum 50-person canary. The implementation will
make that first canary an explicit bounded initial allowance of at most 50
(and never more than the remaining daily budget); subsequent allocations
use the ordinary hourly formula. The same effective cap is used by dispatch,
estimates and simulation. This retains the 150/300/600/1000/1500/1450 daily
example without allowing the example's 100-person second-hour burst.

Reservations count before provider calls. An unstarted reservation that
expires at a sending-slot boundary must be reconciled against its existing
campaign before rows can be reassigned. An uncertain start is never retried
through a new campaign.
