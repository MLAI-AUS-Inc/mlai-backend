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

D2 remains incomplete until Content Factory article B1/B5 outcome delivery and
automatic stale-proposal rebuilding are implemented and tested. Refreshes must
use a fresh owned branch/current base, preserve deterministic consent only and
require a new review for model-generated changes. No rollout or customer actions
are part of this change.
