# E2 backend: hosted attempt identity and partial-build recovery

Implement on the E1 backend stack, with no rollout, database/migration, provider,
customer, approval or merge operation. The CF producer is draft PR #994.

The producer builds `cf-review/<run>` (or its configured prefix), saves that
branch in the protected preview proof, and includes it in C3 v1. The backend
currently compares that field with the original source branch, commonly `main`.
Accept an explicitly saved preview branch only when its source repository and
the receipt/preview branch agree. Preserve historical source-branch validation
when the protected branch field is absent. Keep original source SHA, current
scan, consent, nonce, exact head, render/gates and existing approval guards.
This changes no publication target or base-branch authority.

An E2 bound environment receipt with `identity_complete=false` is a terminal
observation about that attempt. The current automatic path treats the absent
C3 receipt as `setup_preview_verification_required` (waiting), although no
later callback can complete the missing build identity. Introduce an independently
disabled backend recovery flag, retaining the existing hosted/enrollment/consent
boundary. Only an exact review-ready preview plus a well-formed, current, scoped,
digest-bound partial receipt may select recovery. Missing, malformed, foreign,
stale or complete receipts retain the existing handling.

The new reader validates the outer CF binding and its original run/source,
preview branch/head/nonce/builder identity, current repository scope, plan/public
input digests and request source/app-root. A self-digest is not a signature: this
is a negative eligibility observation from the existing authenticated service
result, not new positive build/approval authority. Do not synthesize a C3 receipt
or copy the full CF verifier into Django.

Use the existing backend-owned `prepare_no_progress` strategy code, with a fixed
environment-identity constraint finding. The ordinary goal failure journal,
strategy ladder, replay handling and budgets select the next action. Keep
config-required review and sealed owner review choices ahead of recovery.
Legacy/flag-off behavior stays unchanged. Hosted channel execution still awaits
E5; this increment does not claim the one-shot flow or nightly target is complete.

Validate with database/network-denied tests of actual approval and goal policy
functions: actual temporary branch, contradictory branches/source repositories,
partial/complete/missing/invalid bindings, stale source/head, owner review,
flag-off compatibility, one strategy advance and replay stability. Run the
canonical emitter audit and model/compilation checks. Disposable CI may execute
the repository's established checks; no local test database is constructed.

## Implementation evidence

The temporary branch reader now requires the saved CF proof's repository to
match the run and current website before accepting its explicit branch. It
retains historical source-branch behavior when that field is absent. C3 v1's
closed fields and digest are unchanged. Original source validation and the
approval control remain required.

The partial reader requires the exact versioned outer field set, explicit
`identity_complete=false`, bound status, exact source/head/branch/nonce, builder
run URL and ID, matching plan/public-input digests and request identity, current
connection/repository/generation/app-root and fresh completed preview proof.
It checks CF's ASCII-escaped canonical digest without treating it as a signature
or revalidating the full broker record. Complete/missing/malformed/foreign
records cannot drive this recovery path. Both review choices precede recovery.
The independent `ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED` flag defaults off.

The initial 42-test run exposed a test fixture whose company scope did not match
its operation. Correcting that authority tuple made the actual failure-journal
test pass: it records strategy once, reserves the next rung with a fresh attempt
key, and leaves that key unchanged on replay. The broader run passes 194 tests
across goal, strategy/budget, prepare, hosted, consent, source, failure-contract
and approval suites with database and network access denied. The canonical
emitter audit checks 686 sites with unchanged vendored bytes; all 493 test
modules are assigned to CI with zero omissions. Compilation and actionlint
pass. Remote CI and full cross-service goal execution remain separate evidence.
The model graph check reports no changes without opening a database. The CF
producer at `589b7b83` passed all 43 genuine container cases in 342.53 seconds
on run `38758580215`; its regression CI passed, with full backend CI still
running when this backend change was prepared. That is build/callback evidence,
not a deployed website or full goal/nightly result.
