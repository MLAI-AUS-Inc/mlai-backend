# F2/G4: bound the lifetime of bootstrap's large ORM graphs

## Design

Keep bootstrap's current reads and proof/permission checks. Release only its
request-owned prefetch links after both presentation and live overlays have
finished, including cold/warm, summary/full and exceptional paths. Prefetch steps
only for the final selected run window, so discarded candidates do not acquire
cycles. Rollback is to revert this change; no setting or rollout flag changes.

The corrected application comparison in
[run 39055841107](https://github.com/MLAI-AUS-Inc/mlai-backend/actions/runs/39055841107)
still has high memory after replacing the test fixture's retaining throttle
Mock. It exercises real Gunicorn, the compact bootstrap's Django URL/view/ORM
path and 24 persisted large runs with a forced-auth synthetic owner. This is
application-fixture evidence, not production capacity evidence.

| Request limit / jitter | Completed requests | Worker lifetimes | Peak KiB | Elapsed ms |
| --- | ---: | ---: | ---: | ---: |
| 300 / 50 | 1,200 | 4 | 2,934,652 | 231,174 |
| 1,000 / 100 | 1,200 | 2 | 3,051,384 | 224,595 |

The measured CI memory ceiling was 16,766,414,848 bytes. Observed merge source:
`2a83a0f5042042ade13168977e8e8ddb157154f5`; PR head:
`92cd8731d5e9cdbe11e16ed8a7ab52e6ddfc7fba`. No production host RAM bound is
established by that runner. These data do not justify raising the default.

## Reproduced cause

Django's reverse prefetch stores a per-parent QuerySet, whose known-related
objects map retains that parent. Even an empty `steps` relation forms a cycle.
The parent's large `result` JSON therefore survives until cyclic collection.
Bootstrap's prefetched comment list can also cycle through each comment's cached
run foreign key. A small response does not imply a small retained ORM graph.

A database/network-denying regression invokes **Django's actual prefetcher and
real run/step/comment model objects**, substituting only query evaluation with
synthetic rows. No HTTP client is involved. With cyclic GC temporarily disabled
inside the test, a weak reference proves the empty reverse-prefetch cycle exists.
Six cold/warm/populated/error-path cases then fail before the fix: the completed
bootstrap projection still retains the run. This separates this cause from the
earlier test-only Mock retention.

## Change and boundaries

After the live overlay finishes, a `finally` block drops only bootstrap's `steps`
prefetch and `_bootstrap_component_comments` attribute. This breaks those edges
without forcing GC or retaining a global cache. The helper does not modify
result/request JSON, model fields, proof/approval facts or the database.

Run selection still retains current scan/setup observations and applies the
same organization/domain, connection-generation and reset rules. Step prefetch
moves after selection; consumers still receive the same selected objects with
prefetched steps. The existing busy-history regression now verifies that only
the final display window is prefetched. Explicit full review bodies remain
available. No source/permission authority is cached or skipped.

## Verification and remaining evidence

The seven bootstrap/incident suites pass 81 database/network-denying tests,
including the real-prefetch lifetime regression. Another 61 driver/profile,
runtime, deploy and CI-reuse checks pass. All 600 test modules are fully
assigned. The same remote disposable PostgreSQL memory lane will measure this
change; no after-change memory, speed or capacity improvement is claimed yet.
Existing remote endpoint tests continue to require 24 runs, a complete compact
response below 300 KB, no provider effects/writes and current revocation checks.

Production recycling remains 300/50 and process profiling remains off. Every
Articles rollout flag remains off. No migration, database, service, Gunicorn
worker, model call or framework build was run locally. This draft does not
merge, deploy or change customer/production resources.
