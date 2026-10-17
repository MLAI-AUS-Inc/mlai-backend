# E5: native first-article payment reservation and settlement

Native goals have a separate private setup/price consent and financial authority.
The provider scope, current owner/policy and server-only durable lease are
reloaded before consuming a hold. A JSON goal selector, copied financial receipt,
another payer/spend or a stale lease cannot authorize generation. The native
request-key namespace is distinct from repository goals.

The private canonical generation/charge helpers accept a typed native goal scope.
They reserve the CMS artifact, consume the original payer/ledger/accepted amount
and reserve the original dispatch in the existing transaction. Consumption does
not consult today's price or charge again. Explicit original zero-price holds
are supported; missing paid evidence is never free. Native goal references are
included in the original backend request identity and protected across sparse
worker snapshots. Requests without native references retain their recorded
pre-goal digest, preserving older paid CMS reservations.

## Cancellation and erasure

A separate private native credit outbox snapshots useful generated-work evidence
before lifecycle invalidation erases private handoffs. It locks the organization
before credit/parent rows, checks the original spend, and credits the original
payer once. Missing acknowledgements and database failures remain scheduled.
An actual already-committed credit wins a stale parent receipt. The worker needs
no current provider key, native rollout or owner login to settle previous money.

Useful original drafts retain the generation charge, including when the native
handoff was accepted before the parent ledger woke. Current/historical native
draft proof or an exact private accepted-handoff receipt supplies that evidence.
A copied worker approval or publish flag does not. The original dispatch stores
a minimized private settlement audit, bound to its native goal, request, payer,
ledger and amount. It survives local website/goal erasure and prevents a late
worker refund from forgetting already delivered original work. Child refund
deferral resolves the original backend dispatch even when worker goal fields are
omitted or changed.

The private financial-records marker is retained in CMS lifecycle tombstones.
Disconnect/purge queue settlement before key invalidation; both native and
repository credit outboxes are excluded from generic cancellation. This fixes
the existing case where revocation could cancel its newly reserved credit.
Purge removes native planning/preview data immediately and retains only scoped
financial consent/proofs until settlement. Completed financial parents are
erased. Erasure cleanup waits for both credit types before deleting a revoked
website shell. No published provider article is removed.

## Validation and remaining work

All 1,141 no-database website checks pass: 44 new native consent/hold/settlement/
refund checks and one recorded-request-digest compatibility regression. They
cover the real canonical charge branch, immutable original payer/price, stale
leases, revocation, explicit free holds, cancellation, rollback/retry, missing or
already committed credit, erasure racing parent proof collection and late worker
refunds. The 69-check focused native/repository financial group also passes.
Models/migration dry-run report no changes; 788 failure emissions are audited
and all 564 test modules are assigned.

Disposable CI additionally seeds an actual synthetic native hold, verifies replay
without a new debit, revokes the connection, retains its pending credit, settles
an actual synthetic credit with provider execution disabled and checks purge.
The repository provider-chain suite adds an actual disconnect/credit regression.
Their database execution is remote-only and pending CI. No local database,
actual wallet/provider service, paid generation, merge or deployment ran.

This is the financial foundation, not a public native goal start endpoint.
ARTICLES_CMS_GOALS_ENABLED defaults false, alongside all existing rollout flags.
The native goal itself stays frozen outside generic Git cleanup. Actual native
goal reservation/controller, automatic capture/topic/dispatch/recovery, managed
provider-write/admission guards, budgets/strategy/status/doctor and Chat setup/
review UI remain integration work. Publication recheck, paid revision lineage,
unpublish, hosted fallback and real provider/browser acceptance also remain.
