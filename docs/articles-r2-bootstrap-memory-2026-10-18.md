# Bootstrap memory and optional worker profiling

This is the independent R2 extraction from backend #1125 and #1121, based on
main `7983fff81f1e3421936a3861dd799a422285c03e`. It also reuses the standalone
bootstrap read optimizations from `e2733ec` and `db300af`. No CMS contracts,
article source-refresh authority or D2 controller changes are included.

## Behavior

Django's reverse-prefetched step QuerySet retains its parent even when it has
zero steps. Large run JSON then survives until cyclic collection. Bootstrap now
prefetches only its selected display window and releases its request-owned step
and feedback edges after serialization and live overlays finish, including
exceptional paths. It does not force collection, change persisted rows, cache
ORM objects, or bypass fresh connection and permission checks.

The complete read computes workflow progress once through the live overlay.
Draft cards reuse only matching primary-key and update-time versions already
loaded for that organization; missing or changed versions are still fetched.
Explicit full review responses retain their full content. These optimizations
change the default bootstrap read path and roll back by reverting the change.

Worker hooks add optional process-only RSS/high-water measurements. Both an
absent and false `GUNICORN_MEMORY_PROFILE_ENABLED` produce no process reads or
memory events. Enabled events contain only a closed set of numeric counters
and a fixed phase. No request, response, credentials or exception details are
inspected. Optional measurement/logger failures cannot fail a request. The
configured sampling interval is bounded, defaulting to 100 completed requests.

Gunicorn recycling remains **300 requests with 50 jitter**. Backend #1123's
candidate-limit comparison and #1126's runtime tuning remain held until the
owner has actual production RSS measurements. All Articles rollout flags remain
off. This extraction does not enable production profiling.

## Evidence and CI

The real-Django prefetch regression replaces query evaluation only and proves
the empty relation's cycle with a weak reference. Six cold/warm/populated/error
cases fail against main before the fix and release the large run immediately
afterward, without a collection call. Unit profile tests verify actual Gunicorn
hook signatures, opaque request arguments, absent/false configuration, bounded
sampling and failure isolation.

The existing disposable PostgreSQL job also runs the actual authenticated
bootstrap URL, view, ORM, serializer and live overlay in real Gunicorn workers.
It seeds 24 large synthetic runs and performs 1,200 compact reads at the
unchanged 300/50 limit. The child has only a closed synthetic environment and
uses a private Unix socket; provider calls and reconciliation effects fail.
It exports numeric lifetime summaries, never raw logs or article content.
The regression requires peak fixture RSS below 512 MiB, with headroom above the
historical fixed fixture; this is not a production sizing limit.

The fixture infrastructure is reused from #1123 solely to prove this extraction.
The new scope command requires the expensive lifecycle/application checks for
changed bootstrap, profile, ORM, dependency or fixture paths. Unrelated hotfix
paths skip them. Relevant failures fail the existing PostgreSQL validation job;
there is no new unconditional deployment prerequisite. The exact event base and
tested revision determine scope; an invalid event fails closed. Manual branch
validation compares the branch to current main.

Historical stacked CI reported peak fixture RSS falling from 2,934,652 KiB to
347,216 KiB at 300/50 (runs `39055841107` and `39056996063`). These are synthetic
CI fixture measurements, **not production memory, capacity or latency**. They
are not evidence for this independent extraction's head. Its own full CI and
application fixture must pass before review; record those exact results in the
PR body.

Local validation uses `scripts/test_without_database.py` with explicit module
labels, denying every database connection and network connection. Model drift
is checked read-only. No migration, database, service, Gunicorn process,
framework/customer build, paid call, production operation, release or merge is
run locally. Disposable CI is the only database-backed execution boundary.
