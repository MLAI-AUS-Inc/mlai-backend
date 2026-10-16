# C3: exact hosted build proof and consent-based approval

14 October 2026. Continuous implementation approval applies. Default-off feature
ARTICLES_HOSTED_VERIFICATION_ENABLED owns this change; no live flag is enabled.

A skipped local build is accepted only with an exact-commit hosted builder receipt
created by Content Factory after its authenticated callback passes the saved branch,
commit and dispatch-nonce fence. The receipt contains a fixed version/run/commit/
branch/builder reference/nonce/status/time/digest set, never callback URLs or logs.
Ready/available alone, fallback render, missing receipt, wrong commit/run/nonce,
wrong resume generation, manual overrides and stale source do not authorize approval.
Existing browser/static/dependency/publish-surface gates still have to pass.

Threat model: model output, public preview URLs, customer repository content and
arbitrary provider dictionaries cannot manufacture build authority. CF's server
writes the receipt under the run lock only after its credential-protected builder
callback is correlated to the pre-dispatched identity. B5's durable authenticated
callback and original server request scope bind it to the owning backend run.
Backend validates the receipt digest and identities, the exact preview/browser
proof and the current scan/observed source. It does not fetch arbitrary URLs or
mint customer CI credentials. Compromised platform service credentials remain
outside the trust boundary. The receipt is evidence, never an approval grant.

The hosted verification mode is saved in the original trusted setup request. It
prevents installing a customer workflow and keeps workflow permission optional.
The platform builder still verifies build/output/browser and a live marker is
required after merge. Hosted proof can also allow static historical runs with an
original exact builder receipt; absent evidence still requires owner review.

Review-first consent or organization requires_review always asks review_setup.
Auto-publication consent allows automatic setup approval only after all proof
checks, and only through the existing approval/merge control paths. Advisory style
results follow A1; blocking style/browser/publish failures remain blocking.

Validation: positive static/TalaThrive-shaped receipt, wrong/missing digest, run,
nonce, source/head/resume identity, fallback, review consent and manual overrides;
CF actual signed callback fence/receipt, immutable request mode and workflow intent;
both default-off compatibility and current-source local build paths. No production
callback, merge, deployment, migration, customer domain or rollout.


Checkpoint: the exact hosted build receipt and consent-based setup approval are
implemented and tested. All19 database-free CI commands and90 combined goal/merge/
permissions checks passed;639+2 failure emissions are audited (641 total), with348
fully assigned modules and0 new omissions. CF50 focused callback/receipt/usage tests
and213 broader static/native/inventory/lifecycle neighbors passed; immutable failure
contract bytes and1108 emissions passed. The missing config import caught by the
first callback run was fixed before the passing run.

Post-merge native verification is implemented as a durable read-only child job.
Backend reserves the original operation/parent/source identity, polls its saved
state, and fetches completed proof again through the authenticated service before
promotion. Pending work cannot mark a prepare step complete. Foreign responses,
changed source, cancelled operations and incomplete native evidence cannot promote.
The new authenticated_hosted origin uses the existing full NativeEvidence validator,
reviewed mutation lineage and live marker gate; it needs no customer CI read grants.

CF seals generated native commands in its controller store and runs the existing
controlled CI verifier inside the disposable Docker broker, at the exact merged
commit. Repository subprocesses use uid 1000 and cannot write the controller proof.
The broker strips checkout credentials, binds source/tree identity, and records the
environment and lockfiles. Tracked file bytes, modes and symlink targets are checked
independently of mutable Git metadata. Private build environment values are rejected.
The result persists usage and terminal state, and replay/lost queue delivery reuse
the original job. Adoption also retains the correct installed witness/marker config
and omits customer verifier/workflow files. Required organisation review is shown
up front in Chat and cannot be submitted as automatic publication.

Both independent backend hosted flags and CF's flag remain false. The hosted plan
is offered only when both backend flags are enabled for an enrolled goal. A local
committed static fixture passed the real native renderer checks, including marker,
canonical routes, unknown-slug 404 and both browser widths. Its uid-drop override
was explicitly limited to that local fixture; real Docker isolation tests remain
opt-in and are not claimed as executed. Broader suites are recorded in the progress
log after completion. No customer repository or production service was contacted.

Follow-up correctness checks for C4/C5/F3: pause goal wall-budget time while waiting
for a required owner action (current B4 wall budget counts all elapsed time); capture
usage for model work performed in hosted callback quality gates (current metering
is around admitted task execution). Keep owner waits distinct from provider waits.
