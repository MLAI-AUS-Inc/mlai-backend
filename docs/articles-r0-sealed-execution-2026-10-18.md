# Hosted execution proof v3

Status: implementation, default off. This does not qualify a live site or any completion gate.

The pending proposal and the accepted target are different records. The backend
reads the saved setup's candidate through an authenticated, bounded service
request. The original setup source remains the baseline. Its publishing target
must match the stored target, including the legacy contract digest computed
from the original marker representation. A normalized candidate must never
silently replace that older digest.

Before requesting the candidate, the selected target receives the durable
`requiredProofVersion: 3` capability. This removes article readiness while
retaining the original accepted contract. Scans cannot remove that requirement,
replace the target, or select another default on the worker's behalf. Turning
off the rollout does not restore an older proof for an enrolled target.

The hosted child operation stores the full candidate, accepted-target snapshot,
original setup request digest, parent attempt and consent digest. Its request
identity includes the source and publishing-input digest. Remote calls occur
outside locks. Before reservation and final promotion, the backend repeats
parent, selected target, source, consent, generation and deletion checks.

Only an authenticated response from the configured CF service can supply v3
proof. The locally pinned producer commit determines supported verifier bytes;
the candidate and proof cannot choose that authority. The proof must cover
the exact child operation, candidate, original baseline, source, routes,
lockfiles, builds and browser checks. The backend atomically promotes the
candidate and proof, then annotates the saved proof with its authenticated
origin. Ordinary scans and the public CI-attestation API cannot promote it.

Saved-proof, article-admission and live-deployment readers must all apply the
same protocol. Candidate checksums and old v2 receipts cannot satisfy v3.
Native GitHub v3 remains unsupported until workflow and runner provenance are
authenticated. Legacy setup configs without a candidate require an explicit
migration; they are not reconstructed from a newer scan or treated as verified.

Validation must include real entrypoint fixtures, changed-input and changed-
scope rejection, lost-response replay, cancellation during remote work,
pending-state downgrade attempts, and the actual promotion/readiness/live
paths. Fixture subprocess/browser substitutes prove protocol behavior only.

The producer pin is CF `7e827bf9a236730d1e0b6a6b59d6c6ae733a31c7`
(draft #1103). CI compares the manifest, both entrypoint fixtures and the actual
runner bytes against that immutable revision. This receiver is stacked on
backend #1143 and requires its candidate contract.

Local validation: 870 article-journey tests passed with database access denied,
including 159 focused regressions; all 540 test modules are assigned to CI.
Seven transaction tests are registered in the disposable PostgreSQL lane and
must pass there before this draft is eligible for integration. Live hosting,
native CI provenance, legacy-candidate migration and production deployment
remain separate, unproved requirements. The rollout flag defaults to false.

## Owned post-merge input recovery

The prepare transition used to clear `setup_run_id` before the hosted reader
requested that same reference. A merged setup could therefore wait indefinitely
for verifier inputs that still existed. The transition now retains a separate,
bounded `hosted_merged_setup_source_v1` reference. This identifies private inputs;
it does not restore setup approval or accepted source proof.

Recovery is limited to this active goal's original backend dispatch key, its
captured attempt and retry epoch, unchanged consent and immutable request, and
the backend-owned merge intent/response for the currently observed source.
All present optional goal claims must agree. The sealed reader checks scope,
ownership and source under its existing locks before and after the private
candidate read, then again at reservation and final promotion. The original
candidate and baseline remain unchanged; execution uses a fresh child bound to
the current prepare attempt. Repeating the same post-merge transition leaves
its identity unchanged. A fresh selected/dispatched setup retires the input
reference, while explicit repair or a required new strategy candidate cannot
borrow it.

Missing historical references, conflicting current/merged references, source
movement, changed consent or malformed merge facts remain pending. Worker
callbacks cannot introduce the protected backend merge facts. This slice covers
the worker-owned post-merge transition only: webhook-first recovery and the
independent CF marker-only re-adoption PR churn remain open. No rollout defaults,
customer files, production data or deployment are changed.

Validation: the actual post-merge transition to sealed child reservation failed
before the repair. A separate repeated-transition regression also failed before
its idempotence guard. Strict tests deny database, socket, listener and process
access. They cover parent/client identity, attempt/epoch, all optional goal
claims, merge/source tampering, changed input during the private read, final
fences, callback forgery and fresh setup selection. Disposable transaction and
provider/build/browser qualification remain remote or separately approved work.
The complete article-journey selection passed 987 tests, with 546 test modules
fully assigned to CI and the offline 746-emission failure-contract audit passing.
