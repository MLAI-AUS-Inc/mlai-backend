# One update per reporting month

Implemented locally on 27 September 2026, on top of the queued Startup Pulse
connections change. This is not a deployment record.

## Owner experience

The archive and dashboard show one entry per startup/reporting month, newest
month first, titled `September Update` with month artwork. A record's update or
publication timestamp does not move it to a different reporting month. Months
without any saved content are not fabricated.

Create/update always resolves the selected calendar month. New clients load
`GET /api/v1/community-chat/startups/updates/?month=YYYY-MM-01` and reopen its
current receipt before editing. The month filter also accepts `YYYY-MM`.
Archives apply monthly grouping before their existing 50-item pagination, so
multiple historical September records cannot crowd August off the first page.

Owner detail responses include `previousUpdates`, the earlier independently
saved records from that same startup/month. Clients expose these read-only under
Earlier saved versions. Opening an old owner ID opens the month's latest working
copy. `version=published` still reads the specifically requested approved record.
Historic records, revisions, approval receipts, snapshots, and discussion IDs are
retained; no content is deleted or automatically combined into a new publication.
The current working copy is the most recently updated record (ID breaks ties).
Community archives group only after approved-audience filters, selecting the
latest approved publication; unpublished/private siblings never enter that feed.

## Writes and concurrency

`resolve_update` takes the existing organization row lock before resolving or
creating a month. New month rows use the existing unique nullable-creation-key
monthly slot. Compatibility `creationKey` values cannot create additional rows
in an existing month. An explicit historical ID cannot overwrite a newer sibling.
Saved revision IDs remain mandatory optimistic concurrency receipts: stale or
blank writes to an already saved month return 409 instead of silently discarding
its content. No new database model or migration is needed.

Founder generation now follows this same identity flow even when callers provide
only a target month. Worker writes resolve the same current month, and old raw
memo upserts cannot overwrite revision-backed content. Approved publications
remain unchanged until the founder reviews and approves the replacement revision.

## Source periods

Connector narrative data uses the represented calendar month in the startup's
reporting timezone, `[month start, next month start)`, capped at now for a current
month. Editing an older month includes that whole month, rather than 30 days
before its old publication date or before today. Out-of-month explicit source
ranges are rejected. Provider bundle reads, extracted events, and frozen evidence
are restricted to that month. Financial evidence retains its reporting month;
unknown historical values are not replaced by current values. Existing frozen
revisions stay unchanged until regeneration creates a new reviewed revision.

Historical records remain grouped for display without a destructive data repair.
Deploy the compatible backend and clients together; old clients that try to create
another independent same-month update will receive a conflict and must reopen the
month. PostgreSQL concurrent-write validation and live connector verification
remain rollout checks. No migrations, live database writes, or production provider
calls were performed for this implementation.

## Local verification

109 database-free backend tests passed, covering month identity and lookup,
owner history preservation, audience projection, source preferences, provider
boundaries, source evidence and immutable old-run rejection. Django system checks
and read-only model/migration drift checks pass (`No changes detected`).
Compilation and whitespace checks pass. Database-backed tests were updated but
not executed: repository policy requires separate explicit migration approval
for the Django database test runner. PostgreSQL lock behavior remains unverified
by these database-free tests.
