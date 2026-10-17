# G4: worker-memory evidence before recycle tuning

The runtime script currently defaults to 300 requests with 50 requests of
jitter. G4 requires memory evidence after F2 before increasing that limit.
No sustained application-worker memory profile has been collected in this task.
This increment prepares that measurement and validates the profiling lifecycle;
it does not choose a new production capacity limit.

`GUNICORN_MEMORY_PROFILE_ENABLED=false` is the default. When explicitly enabled,
the worker records a baseline after existing Django URL warming, one sample per
`GUNICORN_MEMORY_PROFILE_INTERVAL` completed requests (default 100), and a final
exit sample. Each JSON event contains only its schema/phase, process ID, request
counts, resident/high-water memory and resident delta from its first available
sample. Missing procfs data produces one fixed unavailable signal per worker;
it is never a zero measurement. Request paths, queries, headers, status bodies,
users and exceptions are not inspected or logged. The interval is bounded and
profiling/logger failures do not fail startup, a request or shutdown.

The Linux [procfs counters](https://docs.kernel.org/6.17/filesystems/proc.html)
are approximate per-process RSS/high-water observations. They do not replace
precise proportional-memory or container/cgroup measurements. Hook signatures
are checked against the locked
[Gunicorn 23 source](https://github.com/benoitc/gunicorn/blob/23.0.0/gunicorn/config.py)
and its installed callable validators.

CI runs a real two-worker Gunicorn fixture with a short request limit and jitter
over a private Unix socket. Its application allocates a small temporary buffer
and returns a fixed acknowledgement. Production request/exit hooks collect real
procfs samples and worker recycling must be observed. The fixture loads no
Django application, account or database. It is lifecycle acceptance, not a
representative application memory or capacity measurement.

The driver requires owned Linux repository CI and existing runner-temporary
storage. Its child receives only a fixed small environment, excluding repository
tokens, database URLs and provider credentials. Logs remain private temporary
files; only closed numeric aggregates are printed. The bounded driver stops its
own process group and removes its temporary files. A request/header canary must
not occur in logs. Nothing listens on a public or loopback TCP port.

Local validation: 41 database/network-denying profile/runtime/CI-reuse tests
pass; all 596 test modules are fully assigned. Syntax and workflow YAML parse.
Actual Linux Gunicorn smoke acceptance must pass remotely. No application
worker, database or service was started locally.

For a later approved measurement, record the release SHA, worker count and
container memory ceiling alongside the profile. Compare equal request windows
under representative traffic after F2, including retained growth and peaks over
multiple worker lifetimes. Use those measurements to propose a larger request
limit with adequate jitter and memory headroom. Default values, deployment
settings and all article rollout flags remain unchanged/off in this draft.

The remaining G4 tuning and before/after application measurements are pending;
no production operation, merge or deployment occurred.
