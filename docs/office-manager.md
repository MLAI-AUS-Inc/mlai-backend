# Retired Office Manager pilot and historical financial evidence

The Office Manager of the Day pilot was retired on 12 October 2026. The backend
no longer exposes claim or preflight endpoints, schedules Office Manager work,
reopens volunteer days, or sends its Slack announcements, confirmations,
reminders and corrections. Office Manager enablement and Slack configuration
are no longer part of normal backend deployment.

## Retained records and ordinary coworking

Every historical Roo migration and Office Manager model/table is retained.
Retirement does not delete bookings, balances, ledgers, claim attempts or
immutable provenance/reviewer evidence. These records may be needed to explain
historical points effects, including ordinary paid coworking bookings whose
purchased/earned allocation was attested by the provenance command.

The Django admin can inspect historical days and assignments but cannot add,
edit, delete, close or reopen them. Standard coworking, meeting-room booking,
Roo Points and coding remain separate features.

A historical Office Manager booking can still be cancelled through the normal
coworking cancellation endpoint using the immutable booking ID. This requires
an isolated `ROO_API_KEY`; broader or aliased service credentials are rejected.
The small `roo/legacy_office_manager.py` helper preserves the exact original
debit/refund checks and purchased/earned bucket split. If a converted booking's
refund cannot be safely reversed, or the refunded points are no longer
available, the entire cancellation fails without changing its booking or
assignment. Cancellation closes the historical day and never reopens it.
Standard cancellation refund idempotency remains in the coworking service.

The compatibility helper does not deliver Slack corrections or retry pending
historical messages. Outstanding provider deliveries or a Public Roo action
outbox must be reviewed and drained as part of the companion retirement before
releasing this change. Do not mark pending delivery records as completed merely
because the runtime has been removed.

## Companion release boundary

Older Public Roo revisions call the Office Manager preflight endpoint during
shared authenticated startup/readiness and run Office Manager action recovery
workers even when new claims are disabled. Retire those consumer paths in Roo
before deploying a backend release that removes the two Office Manager routes.
The backend change does not modify the separately deployed Roo service.

## Historical migration and provenance diagnostics

The manual `audit_office_manager_migrations` command is retained. Despite its
historical name it also checks migration identities/schema for meeting rooms,
boost and microRoo/coding, as well as paid coworking provenance. It does not run
as part of every normal deployment.

```bash
python manage.py audit_office_manager_migrations
```

When investigating pending historical channel deliveries, supply their
reviewed channel with `--configured-office-manager-channel`. The command reads
migration records, schema markers and cross-table invariants; it does not
repair data or apply migrations. An unsafe result requires an operator review,
not an edit to an already applied migration.

The append-only historical chain remains intact: Office Manager migrations
`0034`–`0039`, the `0040` graph merge with ordinary coworking operations, and
all later Roo migrations. Migration `0038` also created the shared scheduler
heartbeat; migration `0041` depends on `0040`. Do not remove this chain to
retire the runtime.

`reconcile_office_manager_provenance` is retained for operator-reviewed
historical purchased/earned allocations. Without `--commit` it validates and
reports the proposed change. Any execution against a persistent environment
requires its own explicit operational authorization.

```bash
python manage.py reconcile_office_manager_provenance \
  --booking-id <uuid> \
  --purchased-microroo <exact-value> \
  --reviewed-by <operator-identity>
```

For refunds already reversed, supply independently audited allocations through
`--reversal-purchased-microroo <assignment-id>:<exact-value>`. The command never
infers an allocation. Reclassifying an old refund retains append-only ledger
and reviewer evidence. Dropping historical tables or changing models remains a
separate migration decision requiring approval for the exact migration.
