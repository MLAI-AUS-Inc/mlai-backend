# G4: compare recycle windows with the actual bootstrap application

The existing real Gunicorn smoke proves hook/procfs/recycle correctness with a
small allocation. It cannot justify a higher request limit. Prepare a separate
owned Linux CI job using only its disposable PostgreSQL database and synthetic
founder/company/wallet with the existing 24-large-run bootstrap fixture.

Run one actual Gunicorn worker over a private Unix socket. Its test-only WSGI
wrapper invokes the existing Django URL/middleware/view/ORM/serialization path
through APIClient with the synthetic owner forced authenticated. Disable only
request throttling in this fixture so rate limits do not substitute for the
measurement. Existing read boundaries deny provider/model/network effects. The
production WSGI entry point and authentication behavior remain unchanged.

Compare equal 1,200-request windows with 300 plus 50 jitter and a candidate 1,000
plus 100 jitter. Keep actual production request/exit hooks and URL warming. Sample
RSS/high-water every 100 requests, retaining each worker lifetime and completed
request count. Require actual recycling and 24 compact runs in every response.
Only fixed numeric aggregates, release SHA and observed CI memory ceiling may be
printed. Private temporary logs, sockets and fixture identifiers are removed.

Require the exact owned GitHub Linux CI context and known loopback test database;
the child receives an explicit small synthetic environment, never inherited
repository/provider credentials. Parent tests create only disposable records in
the runner's existing Django test database. No local database, migration,
Gunicorn, service or HTTP listener will be run. The driver has fixed request,
time and log bounds and stops only the process group it creates.

This establishes a repeatable synthetic application comparison after F2, not a
production workload or capacity certification. Report retained growth and peaks
before choosing any new default. The runtime defaults remain 300 plus 50 and
profiling remains off. All article rollout flags remain off; no merge/deployment
or production measurement is part of this increment.

Local evidence: 52 database/network-denying driver/profile/runtime/CI-reuse tests
pass. They cover rejected local/foreign database admission, a closed synthetic
child environment, exact sample/lifetime accounting, private temporary cleanup,
owned-group teardown including early exit/timeouts, and the required deploy gate.
All 599 test modules are fully assigned. Fixture/helper syntax and workflow YAML
parse. The remote comparison evidence is recorded below; no service or database
was started locally and no recycle default has been changed.

The first actual comparison passed its lifecycle checks, but its memory figures
are excluded: the throttle override used a Mock, whose call history retained
every DRF request and response object graph. A weak-reference regression executes
the actual fixture override and fails before the correction. A plain callback
now bypasses fixture throttling without collecting request references. Repeat
the comparison before drawing memory/tuning conclusions. Export the bounded
per-100-request numeric samples as well as lifetime totals to distinguish warm-up,
retained growth and peaks without retaining or publishing raw requests.

The corrected application lane passes in run 39055841107. Peak worker RSS is
2,934,652 KiB at 300/50 and 3,051,384 KiB at 1,000/100, with 1,200 completed
requests each. Keep the runtime limit unchanged. A separate real-Django,
database-free regression reproduces large-run retention through reverse-prefetch
cycles; the [request-lifetime change](articles-f2-bootstrap-run-lifetime-2026-10-18.md)
will be measured in the same remote fixture. The initial Mock-contaminated
figures remain excluded.
