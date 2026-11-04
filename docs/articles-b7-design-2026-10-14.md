# B7 doctor and goal outcome metrics

14 October 2026. Continuing under the owner's continuous implementation approval.
This branch is stacked on B6 and stays behind ARTICLES_WEBSITE_DOCTOR_ENABLED=false.

One bounded doctor projection lives inside websiteStatus. It exposes only fixed
milestone names, safe proof identifiers/digests/times, causal/effective class and
code, attempts, strategy/history, cumulative usage and limits. Missing measurements
remain unknown. It never returns prompts, provider bodies, source text, credentials,
complete child results or arbitrary receipt dictionaries. Copy diagnostics uses
that same contract. Chat renders the server checks without deriving new readiness.

The inspect command uses the same whitelist/redactor, adding observed head, scoped
child status, CI and deployment receipt references plus attempt/retry epoch. New
reads remain bounded and local; no write or provider call is added to the doctor.
Outcome telemetry adds reason/step/class mix, per-milestone durations and measured
per-goal costs with unknown samples counted separately. These do not certify the
nightly full-flow target; E4 remains its owner. Health reporting excludes owner
waits from overdue work and detects waiting against its actual goal budget.

Tests cover malformed/oversized/cross-scope data, credential canaries, unknown
metrics, stable copy output, every four-state view and overdue owner/budget cases.
No production inspect, metric sweep, migration or notification is run here.
