# D2 old publication observation

The stale first-article controller now continues from private source inspection
to a fresh, read-only GitHub observation. This stage is still behind the existing
default-off recovery and source-refresh gates. Reused intents recheck the shared
goal budget before any further service/provider read; enabling source refresh
alone cannot bypass disabled article recovery.

The backend unions every scoped mutation receipt with the authenticated worker
inspection and its publication references. It retains backend-only, worker-only
and disagreeing records. Mutation identifiers remain namespaced to the connection
and bound to the original paid run or its directly owned publication child.
Stored patch hashes are checked before provider access. At most 32 mutations,
16 pull requests and their bounded branch observations can be read.

After current owner, company, original payment, consent, source and prepare-lease
checks, the backend leases the read for 120 seconds. It creates a fresh
repository-scoped read token outside authority locks. The token never goes to
Content Factory and is revoked after the read, including refused/failed reads.
There is no repository write permission, branch mutation, PR close, auto-merge
change or publishing call. Streaming reads use the pinned byte/deadline helper
and share a 60-second provider deadline. Missing or inaccessible provider facts
cannot establish absence. Private provider bodies and actor details are omitted.

GitHub must identify the selected repository and exact current source branch.
Both sides of every PR must belong to that repository and target the selected
branch. Attribution requires an applied backend receipt for the exact PR URL,
branch and head. A contributor's later head remains unattributed. The observation
retains merged state/merge SHA, pending auto-merge and branch/head disagreements
without treating any as current write authority or final live proof.

A fresh authority and prepare-lease CAS, the exact private inspection and an
unchanged complete ledger inventory are required to commit `publication_observed`.
Owner cancellation and newly arriving mutation receipts lose that CAS. Uncertain
reads wait for the lease and reuse the same original inspection operation and
payment. Fresh observations are reused for five minutes to avoid issuing tokens
on every controller poll. A later effect must recheck current provider facts.

## Remaining D2 stages

Every observation explicitly records `effect_authority: false` and
`absence_proved: false`, even for empty ledgers. The next stage must establish a
backend-owned source fence, revoke old effect credentials, reconcile pending
auto-merge and uncertain/merged effects, and seal rechecked editorial content.
Already-merged articles require ancestry plus exact live-body reconciliation
before any replacement proposal. Deeper owned revision lineage and a distinct
historical read grant for source-fenced cancellations remain pending.

The controller must then reserve separate current execution/delivery, rebuild
against current source under the original paid root, enforce fresh deterministic
or model-review policy, and follow current approval/merge/live lineage. Article
strategy escalation remains separate work. This stage currently waits for that
owned fence; it does not claim end-to-end D2 completion.

## Validation and rollback

Strict database/network-denying tests run the real first-article controller,
private inspection admission, intent/payment logic, read lease and observation
CAS with persistence/provider seams. They cover both ledgers, missing ledgers,
unattributed references, merged and auto-merge facts, foreign repositories,
contributor heads, owner cancellation, concurrent mutation, missing/inaccessible
provider facts, transient/lost reads, budget exhaustion and disabled recovery.
Numeric throttling hints survive the pinned transport; an actual HTTPX 403
rate-limit response remains transient and cannot become an owner access action.
The inspection retry fixture now advances the controller's actual clock and
asserts a second HTTP request after expiry. No schema or migration changes.
The related selection passes 217 tests, including 14 publication-observation
cases. CI fully selects all 602 test modules; the offline canonical failure
audit covers 857 production emissions. The immutable inspection contract and
transport bytes are pinned to immutable producer
`0eb18400cd4caa74812763804cc9904718687e33` and pass local parity verification.

Leave both existing flags false to retain the previous public behavior. No
production/customer, paid model, hosting, merge or deployment operation is part
of this change. Remote full-suite CI remains required.
