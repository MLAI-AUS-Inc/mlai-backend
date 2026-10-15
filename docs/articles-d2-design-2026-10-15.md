# D2 — article recovery

15 October 2026. Implements the backend bounded-resume slice of D2 on D1.
The original goal, paid request, source run and publication child remain the
authority. The new `ARTICLES_ARTICLE_RECOVERY_ENABLED` flag defaults off and
also requires first-article goals, enrolled B1 policy and B4 budgets.

An observed failed or blocked article records its exact run, original operation,
attempt, source SHA, worker generation/state version and B1 occurrence/fingerprint.
The shared failure policy counts the occurrence once. A recoverable outcome
journals a versioned intent before calling the canonical resume control. Duplicate
deliveries preserve that intent and its schedule. Unknown intent formats fail
closed. Denied and cancelled runs never enter this path.

Resume requires the current owning prepare lease, original active owner/company,
current connection generation, unchanged source and matching paired goal IDs.
Its private server argument cannot be selected by HTTP/MCP body fields. Automatic
resumes preserve the human-action count; ordinary owner controls still count.
The existing operation-attempt reservation retransmits a lost response with the
same attempt. A submitted intent waits for a new outcome instead of replaying
the same old failure. No generation call or second Roo charge is introduced.

The controller invalidates old draft/live approval evidence and checks cumulative
usage before resuming. Unknown usage and exhausted budgets stop further work.
The existing shared repeated-failure and strategy policy remains authoritative.
Publication-child failures select that owned child, retaining the original source.

Validation includes actual controller/failure policy with storage/provider seams,
duplicate and lost responses, malformed intents, cancellation, source changes,
rollout disablement, budget/unknown-usage stops and publication failures. A new
PostgreSQL provider-chain variant exercises the actual paid run, observation,
failure accounting, canonical resume, reserved attempt, approval, merge, live
verification and final notification alongside D1's successful chain. Run this
only in the existing authorized disposable CI lane; no migration/model changes.

D2's bounded-resume slice passed all CI checks in #1028, including the new
PostgreSQL paid-run recovery-to-live chain. The next slice adds a separate
server-reserved article delivery marker, validated against that original goal.
It shares B5's durable receipt, ordering and pull reconciliation while accepting
only bounded article outcomes/usage. Worker snapshots cannot enroll a stream or
replace its marker; incoming sparse outcomes preserve richer canonical article
evidence. Setup and article apply flags remain independent and default off.

Local evidence for the delivery extension: 155 backend tests pass, alongside
184 Content Factory delivery/queue/attempt/usage/failure tests in #990. The
schema is pinned to immutable Content Factory revision e56b8c50. CI also now
tests server enrollment and actual durable receiver application in the real
PostgreSQL paid-run recovery chain, with a no-op refusal variant.

That chain exposed the zero-based worker generation and nullable usage schema
gaps; both are fixed without changing the resume-ordering guard. A refused
canonical resume cannot mark the goal's repair submitted. It raises a bounded
strategy outcome instead. Content Factory's reserved-attempt repair path checks
the original goal budget and authenticated operation before overriding only the
legacy non-retryable label; source shortfall/admission/budget guards stay active.

At cab8dc31, all five PostgreSQL provider-chain cases passed, including actual
durable failure reception, paid-run recovery through live and refused-resume
handling. The ordinary CI check then reached the existing selection-debt review
deadline. The follow-up assigns 123 omitted and ten partial modules to explicit
lanes (13 database-free; remaining modules in the existing disposable PostgreSQL
job) and retires the empty baseline without extending its review date. The 120
newly selected database-free tests pass. Assignment is complete; the newly
selected database-backed suites still require CI execution and repairs.

The restored PostgreSQL command ran 1,697 tests at 495734dc and exposed 188
failures and 59 errors. At ffcff89c it reported 162 failures and 87 errors;
the initial tenant-fixture repair exposed duplicate legacy organisation creation.
Subsequent batches reuse the owned organisation, bind callback/dispatch fixtures
to real website records, keep monthly draft and source windows aligned, replace
old dispatch/readiness expectations with current contracts, and restore current
migration leaves after the historical microroo test. The latest fixes are in CI;
the full suite is not yet green. No test modules were removed or skipped.

Additional repairs keep HealthHack's administrator-only boundary, supply the
reconciliation organisation, stub the current Slack provider method, require
active ABR evidence in registration fixtures, and preserve imported financial
metrics instead of manufacturing revenue through an editor request. Purchase
tests follow the existing authenticated/positive-only policy; no billing policy
or purchase implementation changed. These DB tests are syntax checked locally
and execute only in the existing disposable CI lane.

Subsequent CI evidence: 393eb315 reduced the restored suite to 120 failures
and 47 errors; e1e72508 ran 1,700 tests with 104 failures and 44 errors. The
notification endpoints, readiness suites and monthly source-window cases now
pass, including the new current/wrong-repository/stale proof cases. The next
batch preserves consent inside nested callback requests so identical deliveries
remain unchanged, tests explicit keyed rejection separately from ambiguous HTTP
refusal, and runs the complete callback/SQLite-lock suite in the existing SQLite
lane. All 490 modules remain fully assigned. Later-head CI is still pending.

D2 remains incomplete until automatic stale-proposal rebuilding and article
strategy escalation are implemented and tested. Refreshes must
use a fresh owned branch/current base, preserve deterministic consent only and
require a new review for model-generated changes. No rollout or customer actions
are part of this change.
