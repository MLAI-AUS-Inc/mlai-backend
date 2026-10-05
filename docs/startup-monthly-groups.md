# Monthly updates and additional copies

Implemented locally on 3 October 2026. This is not a deployment record.
The existing independent-update schema is reused; no migration is required.

## Identity and owner experience

An update represents a calendar month in the startup's reporting timezone.
Clients default to the current month. Before creating another update for a month
with saved content, the client offers to edit an existing update or create a new
one. Every saved copy has an independent ID and revision/approval receipts.

`creationKey` allocates an independent copy, including within an existing month.
A retry using the same key returns that exact ID; reusing its key for a different
month returns 409. `updateId` always edits the requested owned ID, including an
older sibling. Requests without either field retain compatibility by resolving
the latest working copy for the specified month. IDs from another startup are
rejected, and revision conflicts still return 409.

The server assigns `monthSequence` under the existing organization row lock and
stores it in `_month_sequence` metadata in the existing draft memo. The shared
owner/community response adds `updateTitle`: `October update`, `October update
#2`, and so on. New numbers are the highest stored number plus one for that
startup and year/month. Legacy siblings are numbered in ascending ID order while
avoiding existing allocated numbers. Editing, generation and retry preserve the
allocated number. Deleting a lower-numbered copy does not renumber instances
that already have stored numbers. Until its first allocation, a legacy instance
uses a display fallback based on surviving IDs. Deleting the highest-numbered
copy can allow that number to be used again. A
durable deleted-number ledger is outside this code-only change.

Owner and community archives retain every independent copy, with the existing
50-item pagination. `month=YYYY-MM` or `YYYY-MM-01` filters the owner archive
before pagination. Owner detail reads open the exact requested ID; `version=
published` continues to read its approved revision. `previousUpdates` retains
other same-month records for older clients, without redirecting the selected ID.
Community archives still filter exact approved audience/hash receipts before
returning copies; private siblings remain private.

## Generation and evidence

Generation pins its exact update ID, creation key and base revision. Worker
writes and retries continue to use that identity when another same-month copy
exists. Approval is still bound to exact revision ID/hash and audience. Unapproved
edits leave approved publications unchanged; raw memo upserts cannot overwrite
revision-backed updates. Completion rewards are idempotent per approved update. The first completed update
in a Melbourne calendar month earns 20 Roo points when the startup passes ABR,
name and website checks; all other new updates earn 5. Editing or approving the
same update again never earns more. Reporting months do not choose reward months.

Connector narrative evidence uses the represented calendar month in the
startup's timezone, `[month start, next month start)`, capped at now for a current
month. Editing or creating another copy does not extend the represented period.
Provider bundle reads, extracted events and frozen evidence retain those bounds.

## Charts and other consumers

Revenue/cost totals must not be added once per update. The progress dashboard
continues to select one latest verified observation per metric/scope/month.
Frozen metric history selects one snapshot per month by latest reporting cutoff,
with publication order breaking ties. Repeated same-month copies therefore do
not create extra time points or double financial totals. Each saved update keeps
its own frozen chart and disclosures; an unapproved copy cannot replace an
approved chart in community readers.

`monthly_representatives` remains available for period summaries and prior-month
generation context. It is no longer used to hide independent copies from the
owner/community archives. Source selection, chart disclosure and monthly reward
eligibility keep their existing contracts.

## Validation

Database-free identity/archive/review/source tests, model checks and compilation
are safe local gates. No schema changes, migrations, database-backed tests, live
provider calls or deployment are performed as part of this change. PostgreSQL
concurrent creation and live Django/Valley/client journeys remain acceptance
checks under the repository's database-test approval policy.
