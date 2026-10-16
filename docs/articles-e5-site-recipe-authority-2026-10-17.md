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
current generation's target at its reserved source. Require the original run,
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

Reservation creation/enrollment, CF v2 enqueue, reconnect scheduling and the
opportunistic writer/runner upgrade remain separate acceptance work. Do not copy
historical target capabilities or relabel old evidence into a new generation.
Tests use database-free model boundaries and the actual authorization view; no
local database or migration is run.

Validation: 94 database-free authority/hosted/permissions/status/goal/recovery/
fingerprint checks pass. The actual authorization view adds a receipt only for an
explicit v2 request, and a denial returns no partial receipt. Ordinary v1 reads
are unchanged. The canonical failure audit covers 701 production emissions.
Compile and diff checks pass. This remains a gated receipt foundation; no goal
reservation creator or reconnect path is switched to v2 yet.
