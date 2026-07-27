# Organisational memory: daily reconciliation and source health

## Outcome

PR18 makes the existing `memory-scheduler` the authoritative daily safety net
for every active Admin Brain connection. Webhooks remain low-latency wake hints;
the daily run still reads the provider cursor/inventory and reconciles changes,
deletions, and access loss. Public Roo receives no new credential or endpoint.

Landing this code does not enable a provider. The deployment allowlist,
organisation enablement, reviewed scopes, and connection approval gates still
apply.

## Daily state machine

At the configured local hour, each scheduler tick performs this bounded flow:

1. Get or create one `MemoryDailyReconciliationReport` per organisation and
   local calendar date.
2. Inventory every active connection, including malformed active connections
   with no selected scope so they cannot disappear from health reporting.
3. Renew a due Google Drive channel or configured Gmail mailbox watch. Provider
   notifications remain hints and never become memory evidence.
4. Reuse a successful or in-flight sync from the same daily window, otherwise
   create one idempotent `daily-reconcile:<date>` sync action.
5. Let the existing cursor-safe worker run the provider adapter. An outage does
   not skip a date permanently: the first scheduler tick after the daily hour
   creates the missing report and queues catch-up work.
6. Revisit a running report on later ticks. It becomes `completed` only after
   every connection is current and healthy, or `degraded` with content-free
   alerts and an explicit operator action.

A completed empty sync is recorded as `noop`. Re-running the scheduler on the
same day reuses the report and action, so an unchanged provider cannot create a
second daily sync or duplicate downstream work.

## Health and alerts

`MemoryConnectionHealthSnapshot` stores no source body. It reports:

- credential, watch, schedule, and freshness state;
- provider interval and freshness SLO;
- last requested/successful sync and source lag;
- selected scopes, page/record/removal counts, queue depth, and dead work;
- catch-up state and one explicit operator action.

Alerts cover unhealthy credentials/watches, missed freshness SLOs, failed daily
syncs, dead work, overdue reports, unconfigured cost rates, and deferred work.
The organisation-scoped endpoints expose the latest report:

```text
GET /api/v1/org-memory/health
GET /api/v1/org-memory/connections/<uuid>/health
```

The global health response includes only the report summary and alerts. The
connection endpoint includes that connection's latest content-free snapshot.
Django admin exposes reports, snapshots, daily ledgers, and reservations as
read-only records.

## Provider schedules and freshness objectives

The global interval and SLO default to 86,400 seconds. Provider maps are JSON
objects, and a reviewed connection may override either value in its protected
`configuration` JSON with `sync_interval_seconds` or
`freshness_slo_seconds`.

```text
ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400
ORG_MEMORY_PROVIDER_SYNC_INTERVAL_SECONDS={"slack":3600,"linear":3600}
ORG_MEMORY_FRESHNESS_SLO_SECONDS=86400
ORG_MEMORY_PROVIDER_FRESHNESS_SLO_SECONDS={"slack":900,"linear":600}
ORG_MEMORY_DAILY_RECONCILIATION_TIME_ZONE=Australia/Sydney
ORG_MEMORY_DAILY_RECONCILIATION_HOUR=5
ORG_MEMORY_DAILY_RECONCILIATION_ALERT_SECONDS=3600
ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS=86400
```

Use measured provider volume and rate limits before choosing shorter schedules.
The daily report must finish with every connection healthy before downstream
summaries or digests can claim current data. PR19 now enforces that invariant
and creates a content-free blocked digest for degraded reports; see
`docs/org-memory-review-summaries-digests.md`.

## Cost ceiling

Source sync, deletion, and permission reconciliation are never blocked by model
budget. The worker applies the ceiling only to embedding, extraction, and
consolidation work. Before claiming one of those jobs it atomically reserves a
conservative estimate in `MemoryDailyCostLedger`; completion consumes the
reservation and a terminal failure releases it. A job that would exceed the
ceiling is left pending until the next local budget window.

```text
ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD=0
ORG_MEMORY_EMBEDDING_COST_AUD_PER_MILLION_TOKENS=0
ORG_MEMORY_MODEL_INPUT_COST_AUD_PER_MILLION_TOKENS=0
ORG_MEMORY_MODEL_OUTPUT_COST_AUD_PER_MILLION_TOKENS=0
ORG_MEMORY_CONSOLIDATION_ESTIMATED_INPUT_TOKENS=4000
```

Zero ceiling means monetary gating is disabled. Before enabling a non-zero
ceiling, configure all three reviewed AUD rates; otherwise metered work fails
closed and the health report raises `cost_pricing_not_configured`. Estimates
are deliberately rounded up. This release charges the reservation estimate as
the consumed amount; replacing that with provider-billed actuals requires a
separately reviewed pricing/usage integration.

## Operator commands

```bash
# Normal scheduler tick; daily coordination runs automatically when due.
python manage.py schedule_memory_work

# Exercise today's coordinator before the configured hour.
python manage.py schedule_memory_work --force-daily

# Restrict daily reporting to one organisation.
python manage.py schedule_memory_work --force-daily --organization-id <id>

# Queue/runtime maintenance without touching the daily report.
python manage.py schedule_memory_work --skip-daily
```

## Rollout checklist

1. Apply migration `0017` with the web, worker, and scheduler code from the same
   release.
2. Leave `ORG_MEMORY_ENABLED_PROVIDERS` unchanged while validating report
   creation in a non-production organisation.
3. Configure timezone, off-peak hour, provider intervals, freshness SLOs,
   watch callback/Pub/Sub settings, reviewed prices, and the AUD ceiling.
4. Run `schedule_memory_work --force-daily --organization-id <id>` and drain the
   worker queue.
5. Require a completed report, no unexpected alert, exactly one daily action
   per connection, and an empty second sync before expanding the pilot.
6. Test scheduler downtime across the daily hour and confirm the next tick
   records `catch_up=true` and completes the missed reconciliation.

No provider was enabled, deployed, or contacted as part of the local
implementation and test suite.
