# D2 stale article inspection coordinator

The real Git first-article controller now enters a separate inspection stage
when its original paid article targets older source. Both article recovery and
`ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED` must be enabled; defaults remain off.
The current owned merge continues through its existing live path.

Canonical billing and article lookup prove the original owner, company, goal,
spend and absence of refund. Trusted source-only revision rows prove unchanged
repository, branch and app root through the newly verified source. The original
article can be completed, failed, blocked or awaiting owner review; cancelled or
denied originals remain refused. Inspecting a review wait grants no approval.

Under the prepare lease and authority transaction, the controller journals one
stable proposed UUID and reserves `article_source_refresh_inspect`. Its permanent
read-only payload marker prevents generic credential, publication, merge, setup
and cleanup authorization even if the workflow name is changed later. There is
no lifecycle run placeholder, delivery stream, generation request or new debit.

The existing service-authenticated authorization endpoint admits only the exact
private request digest and reservation, after freshly rechecking owner, consent,
current scope, source and original payment. It returns `permission_mode: none`
and empty capabilities. No credential crosses this boundary. Remote calls occur
outside authority locks and use bounded streaming JSON with a wall-clock deadline.

A 60-second leased read suppresses overlapping or uncertain requests. A lost
reply retries the same read operation and proposed UUID after the lease expires.
The response is committed only after a fresh authority and prepare-lease CAS.
Owner cancellation, source movement, stale lease or changed payment cannot
commit the observation. A later usage-only state version is a minimum observation,
not a new article identity; generation and operation identity remain exact.
Content must still be fingerprinted again when a future durable seal is created.

## Current stopping point and remaining D2 work

The durable stage is `inspected`. The controller currently waits for original
publication reconciliation; this is not end-to-end source refresh. The worker
response exposes partial, pending-review and superseded states, old publication
attempts and directly owned child references. Empty mutation history is not
provider proof. The next stages must authenticate old mutations and GitHub facts,
resolve uncertain or already-merged effects, fence old write authority, recheck
and seal inputs, and reserve a separate execution operation and delivery stream.

Execution must preserve original editorial content and one paid root while
building against current source. It requires fresh exact build/render proofs,
review for model-written changes, and current replacement lineage in approval,
merge and live readers. Source-wakeup cancellation needs a separately recorded
server-owned fence before historical cancelled artifacts can be read. Owner
cancellation must never be reinterpreted as that fence. Deeper owned editorial
revision lineage and article-specific strategy escalation remain pending.

## Provenance, validation and rollback

CI verifies exact contract, transport and synthetic example bytes against the
immutable private Content Factory producer commit using the existing read-only
contract secret. Offline checks validate local bytes; they cannot establish
remote provenance. The new tests run in the strict database/network-denying lane.
They exercise the real first-article controller, billing, private admission view,
lease recovery, cancellation, generic capability refusal and actual HTTPX byte
and wall-clock bounds, with only persistence and service seams substituted.
The combined related selection passes 203 tests, including 17 coordinator,
transport and producer-provenance cases. All 601 test modules are fully selected
by CI; the offline failure audit covers 855 production emissions. Local contract
bytes pass verification against the pinned producer lock. Remote provenance and
the full disposable CI suites remain required.

Leave `ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=false` to retain the existing
controller path. The Content Factory flag is independently false. No model or
migration is added. No production/customer, paid model, merge, hosting or
deployment operation is performed by this change.
