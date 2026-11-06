# B3 strategy pins and B4 goal budgets

14 October 2026. The owner authorized continuous design and implementation.
This work is stacked on B2 and keeps independent rollout flags off.

The backend chooses one of `adopt`, `native_collection`, `deterministic`,
`model_adapter`, `agent`, then `hosted`. The current source inventory, scoped
proof digest and recorded failures determine the first eligible rung. Unknown
facts do not certify eligibility. Every pin binds the goal scope and source,
includes a revision, and leaves a bounded change history. Rechecks retain it;
a changed source retains or advances its rung with an explicit history entry.
Setup dispatch carries the server-selected pin through the existing request
and operation guards. Clients cannot supply goal strategy authority.

A fresh, causal strategy failure advances exactly one rung with a new attempt
key. Delivery replay cannot move it twice. Failed children remain historical;
new attempts do not resume them. CF must honor the rung and preserve evidence
when exhausted. The hosted rung requires E5: until implemented, the goal waits
for one channel-selection action rather than pretending a provider is available.

Budgets are independent of dispatch delivery attempts and customer billing.
Defaults are 48 hours wall time, 7,200 active seconds, 20 transient occurrences,
6 repairable occurrences, and a configurable model USD ceiling. The model ceiling
uses cumulative CF journal upper bounds. Unknown costs stay unknown; they never
become zero or establish a within-budget certificate. Calls, tokens, provider
seconds, builds and previews are reported from durable records. Backend snapshots
are deduplicated by original run and monotonic report identity.

The same fingerprint twice escalates a repairable outcome to strategy, and a
repeated strategy outcome to unsupported/channel fallback. Transient outcomes escalate to repairable, then strategy and unsupported when
their fingerprint repeats; alternating fingerprints remain bounded by their
class/time budgets. A first repairable outcome retries automatically on the same
rung with a new attempt key. Waiting and
owner actions do not consume failure attempts; active work is metered separately.
Budget exhaustion stops before another dispatch, with one diagnostic action and
a durable alert slot for F3. Causal B1 envelopes stay unchanged; the effective
policy decision is recorded separately.

Tests cover every rung, same-pin retry, forced advancement, duplicate callbacks,
source binding, terminal history, alternating and repeated failures, class limits,
wall/compute/cost limits and missing meters. Full fixture zoo certification belongs
to E4; these tests do not certify unsupported site frameworks.
