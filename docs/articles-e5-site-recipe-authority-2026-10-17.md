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

The existing runner upgrade and live fixture lifecycle remain separate acceptance
work. Do not copy
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

V1 retains its setup-lineage requirement. Explicit v2 dispatch and reconnect use
the committed recipe and a fresh reservation without an original setup parent.

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

## Reconnect enrollment and durable verification

On an explicit reconnect to the same immutable repository, branch, application
root and site, retain only a previously certified v2 definition. Select the prior
default, or the sole certified target in the latest historical generation when
disconnect cleared the selection. Ambiguous or malformed targets keep the old
scan/setup path. Project a closed definition: no prior proof, source, readiness,
capability, consent or credentials crosses the generation boundary. The new
target starts provisional, with no source SHA or verification timestamp.

Reserve a durable source-reverification operation for the exact previously
certified source and reference the original target row separately. Reuse the
hosted verifier and live-deployment checks, with no setup PR, model planning or
paid generation. The hosted child privately references this reconnect operation;
callers cannot submit that reference. A changed provider head is superseded and
must follow the existing changed-source policy.

For this exact unchanged commit only, the original certified target supplies
source-review provenance. Validate the pending reconnect operation, current owner,
repository/branch/root/generation/deletion epoch, exact target/recipe/native digests
and original row before accepting that provenance. It supplies no build evidence:
the current hosted job must complete and pass the existing source/tree/environment/
render/marker checks. Ordinary changed-source verification keeps its review gate.
Cancellation/rebind fences and bounded retries remain in force throughout.

The implementation uses the existing durable `source-reverify` processor and
hosted verifier. Pending work retains its child reference; an accepted build is
reused while deployment waits. Deterministic build failures stop, and the 24-hour
window is checked before new remote work. Deployment uses the existing bounded
minute schedule. Current status includes the owner-initiated reconnect operation.
The worker's authorization and final backend promotion both recheck the saved
recipe/probe and reconnect reservation, so changed configuration or cancellation
during a completed-proof read cannot promote readiness.

Local evidence includes actual fresh hosted-proof promotion from a provisional
target and rejection of changed source/recipe, plus routing, retry and budget
checks. The actual bind/disconnect transactions are covered by a new CI-only
disposable-database test that reconnects twice and asserts an unchanged recipe,
new empty proof state and no setup workflow. Its remote result is still required.
This does not establish the E4 live lifecycle or acceptance of a source changed
while disconnected. Neither rollout flag is enabled.

## Fresh inventory and templates after reconnect

Do not copy archived generation templates or component caches into the new
generation. First obtain current-generation hosted build proof. Then use the
existing guarded source-rescan dispatch once at that same source, retaining its
durable identity. The already-certified target is protected by the existing
`protect_certified_config` and `target_update_allowed` guards while the scan
refreshes templates, component inventory and source facts. A scanner's weaker
candidate cannot replace the selected certified target.

Keep reconnect pending until that exact current-generation scan has completed,
its operation is still valid and the config records the same source with valid
article/design templates. Failed/cancelled/mismatched scans do not complete
reconnect or cause an unbounded sequence of new scans. Only then run live
deployment verification and complete the reconnect receipt. The original
24-hour verification budget covers all these stages; there is no article charge,
setup PR, copied old proof or new goal-consent seal.

This stage is integrated into the existing source-reverification processor. The
first current build reserves a subsequent read-only rescan; only then can that
scan refresh the component library. Its exact run/request/reservation, generation,
repository, branch, root and source are checked, with both setup-mutation flags
required false. Completed scans must record the same source and valid article and
design templates. A pending local scan does not redispatch models or builds, and
uses a 30-second local observer; deployment retains its slower existing schedule.

All 830 cases in the exact database-free website CI group pass after this stage.
The run includes the previously failing current-operation and owner-merge recheck
regressions, actual fresh hosted promotion, read-only scan admission, invalid/stale
template rejection and the real certified-config selection guard. The failure
audit validates 715 production emissions. Remote database and live fixture
acceptance remain distinct required evidence.
