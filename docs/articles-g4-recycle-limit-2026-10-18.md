# G4: raise recycling after the application memory fix

## Design and user effect

The fixed compact-bootstrap application has stable memory through the measured
1,000-request lifetime. Raise the shell default from 300 plus 0–50 jitter to
1,000 plus 0–100 jitter. Keep explicit environment overrides, worker count,
timeouts, URL warming and default-off profiling. This reduces avoidable worker
replacement while retaining randomized staggering. No service is restarted or
deployed by this change; every Articles rollout flag remains off.

This draft depends on the [bootstrap run-lifetime fix](articles-f2-bootstrap-run-lifetime-2026-10-18.md).
Do not deploy the larger limit without that fix. Rollback can set
`GUNICORN_MAX_REQUESTS=300` and `GUNICORN_MAX_REQUESTS_JITTER=50` at an
owner-authorized release, or revert just the new defaults. Keep the lifetime fix.
Existing configured values continue to win; deployment does not overwrite them.

## Actual application evidence

Both comparisons use real Gunicorn and the same Django compact-bootstrap
URL/view/ORM path, with 24 persisted large synthetic runs and a forced-auth owner.
Each window completes 1,200 requests with one active worker and actual recycling.
Provider/model/reconciliation effects are forbidden. This is not a production
workload or a host-capacity certification.

| Code | Limit / jitter | Peak RSS KiB | Lifetimes | Largest observed growth after request 100, KiB |
| --- | --- | ---: | ---: | ---: |
| Before lifetime fix | 300 / 50 | 2,934,652 | 4 | 744,488 |
| Before lifetime fix | 1,000 / 100 | 3,051,384 | 2 | 803,208 |
| After lifetime fix | 300 / 50 | 347,216 | 4 | 5,188 |
| After lifetime fix | 1,000 / 100 | 347,248 | 2 | 5,260 |

The corrected before comparison is
[run 39055841107](https://github.com/MLAI-AUS-Inc/mlai-backend/actions/runs/39055841107),
observed merge source `2a83a0f5042042ade13168977e8e8ddb157154f5`.
After is [run 39056996063](https://github.com/MLAI-AUS-Inc/mlai-backend/actions/runs/39056996063),
observed merge source `8f2dd70c821a938c4a058ac5eb4e51056a888438` and PR head
`93846122c9aa315af079139c2ba8ede18588be22`. The same measured CI memory ceiling
is 16,766,414,848 bytes. The longer after-fix lifetime completes 1,054 requests,
with samples at every hundred. Peak memory stays about 339 MiB; the larger
request window adds only 32 KiB to the comparison's peak.

After-fix elapsed times are 233,380 ms and 228,755 ms respectively. These single
synthetic windows establish no latency-distribution or general speed-up claim.
The first throttle-Mock-contaminated comparison remains excluded. A repeated
corrected pre-fix comparison on the palette branch also observes approximately
the same multi-GiB peaks; the real-Django weak-reference regression reproduces
the cycle without any HTTP test client.

## Regression guard and validation

Keep both fixed application windows in CI. After exporting their closed numeric
measurements, require each peak no greater than 1 GiB, growth after request 100
no greater than 128 MiB, and candidate peak within 128 MiB of baseline. These
generous fixture budgets catch the prior retention without treating
missing/boolean measurements as zero. They do not grant production admission or
set container memory limits. The larger lifetime must not silently hide a
materially worse memory profile.

All 145 focused database/network-denying checks pass. Offline checks replay the
actual before/after numeric measurements through this guard. An execution test
invokes the real start script using a non-worker `gunicorn` stub and a closed
synthetic environment. It fails with the previous defaults, then verifies the
new defaults and explicit overrides; no Gunicorn or database is started.
Existing runtime, deployment, profile, bootstrap, proof, selection and incident
suites remain in the run. Remote full CI and the budget-enforcing application
comparison must pass on this final draft head.

No migration, local database/service/worker, paid model, customer build,
production measurement, merge or deployment is part of this change. Production
RAM, other endpoint workloads and real p95 capacity remain outside these data.
