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

C3 IS NOT COMPLETE. Post-merge hosted native attestation remains to implement. The
new ARTICLES_HOSTED_ATTESTATION_ENABLED flag is false and controls the setup plan's
default verification mode independently of ARTICLES_HOSTED_VERIFICATION_ENABLED.
Until native hosted attestation is implemented and validated, the plan keeps the
full in-repository grants and existing CI path. Never reinterpret an unavailable
measurement or a build-success field as all native-render checks. Never claim a
branch-head build is an exact merged-source build without new accepted proof.

Existing reusable primitives: CF native_tasks.py, website_lifecycle_api.py,
harness/native_integration.py, harness/primitives.py (WorkspaceRunner isolated
build/preview), native_ci_contract.py and ci_templates/articles_verification.py.
Backend website_verification.py owns full NativeEvidence identity, source/tree/
environment/lockfile/artifact checks and authenticated_ci receipt origin. C3 must
add a distinct authenticated hosted origin and original operation fences without
weakening any required proof booleans or readiness predicates. Build the actual
post-merge verification, cancellation, usage and callback recovery path; no fake
zero measurements, inferred render success or arbitrary model result authority.

Follow-up correctness checks for C4/C5/F3: pause goal wall-budget time while waiting
for a required owner action (current B4 wall budget counts all elapsed time); capture
usage for model work performed in hosted callback quality gates (current metering
is around admitted task execution). Keep owner waits distinct from provider waits.
