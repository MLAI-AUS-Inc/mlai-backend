# E5 / 2.3: current authority outside the committed site recipe

The continuous implementation instruction authorizes this design and its
implementation. Keep rollout off. This work creates no migration and performs no
customer, production or deployment operation.

Content Factory #1065 defines the stable v2 site recipe and a worker that checks
its committed bytes before repository commands run. The backend must supply the
current connection, generation, target and operation separately. A caller's
recipe digest cannot enroll a target or select arbitrary build inputs.

Extend the existing service-authenticated `connections/authorize` read for
`action=custom_contract` only when a v2 verifier explicitly requests it. Under the
existing authority lock, read the saved hosted-verification reservation and the
current generation's accepted target definition. Require the original run,
operation, attempt, deletion epoch, repository, branch, app root, target digest
and site-recipe digest. Return a bounded projection of that accepted target, the
current binding and a probe previously selected by the backend reservation.
Accept no commands, probe or target definition from the query. Perform no remote
call or database write while producing this receipt.

Gate the extension behind `ARTICLES_SITE_CONTRACT_V2_ENABLED=false`, existing
hosted-verification flags and goal enrollment. Ordinary v1 authorization stays
unchanged. CF must reauthorize after reading the pinned recipe and after the
build. This receipt authorizes only the current read/build; existing publication,
provider-proof, source-review, cancellation and consent gates remain required.

Reconnect scheduling and the existing
runner upgrade remain separate acceptance work. Do not copy
historical target capabilities or relabel old evidence into a new generation.
Tests use database-free model boundaries and the actual authorization view; no
local database or migration is run.

The existing hosted dispatcher now selects v2 only from an already enrolled,
current-generation target definition. The selected probe participates
in the reservation identity and is saved privately on the operation. It is
removed from the CF API body, which carries only the explicit protocol version
and recipe digest alongside the existing request fields. Replays cannot replace
the private probe. Legacy targets and disabled flags retain the v1 selection.
The reserved source belongs to the current operation. A target definition may
have been recorded at an earlier source; this read/build receipt returns no
historical source SHA, verification or publishing capability. CF must read the
matching recipe and run fresh verification at the reserved commit. Existing
source-review and publication promotion gates remain in place, and the old
target source/proof is never relabeled. Requiring the old target source to equal
the commit awaiting verification would prevent post-merge verification itself.

The existing setup lineage requirement remains; reconnect without a new setup
still needs its own scheduling and enrollment integration.

## Native target ingestion compatibility

CF's final native target contains its marker and native digest in the nested
`contract` object, while backend verifier and activation consumers read them at
the target root. Ingestion now validates the canonical identity before exposing
those two fields at the root. It rejects conflicting non-null claims, retains
the complete descriptor and preserves all source, provider-proof and readiness
gates. Legacy root-only and custom-contract targets retain their existing paths.
No publishing capability or verification state comes from this normalization.

Both services hash only the four stable marker fields, excluding adoption
diagnostics. Pinned producer examples cover static diagnostic markers and opaque
Unicode target identifiers. CI verifies exact vendored bytes against an immutable
CF commit using the existing read-only contract token. Static probes can carry
their existing detail source path/hash outside the site recipe; authority checks
validate these bounded inputs without reusing a source proof.

Validation: 172 database-free authority/hosted/permissions/status/goal/recovery/
fingerprint/ingestion checks pass. The actual authorization view adds a receipt only for an
explicit v2 request, and a denial returns no partial receipt. Ordinary v1 reads
are unchanged. An actual dispatch-to-authorization test checks the saved private
probe and closed API payload. An actual ingestion test stores the validated
identity while the target remains unverified and publishing stays unavailable.
The canonical failure audit covers 704 production emissions. Compile and diff
checks pass. This remains gated: no production target is enrolled and no reconnect
path is switched to v2 by this change.

## V2 without a setup parent

Choose the accepted current v2 definition before requiring a setup lineage ID.
Only explicit v2 requests may omit that ID: the fresh reservation owns the probe,
and CF reads the committed recipe at the reserved source. V1 continues to require
the original same-generation setup. Do not invent a parent ID or relabel an old
run. Completed hosted proof still has to match the saved request, exact source,
current operation/generation and all provider/source-review checks. This removes
one transport dependency; reconnect enrollment and owner-source review remain
separate work before reconnect can be claimed complete.
