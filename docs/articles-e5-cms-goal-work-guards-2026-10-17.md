# E5: managed native work guards and public-proof recovery

Managed native goals now have a separate current-work boundary. It resolves the
original backend dispatch, current private goal/policy/owner, original held
payer/spend and native budget. Stopped, superseded, completed, review-waiting,
refunded or malformed goals cannot start model work or reserve/use a new CMS
POST allowance. Missing native budget evidence fails closed. Ordinary CMS
articles keep their existing admission behavior.

The native budget uses B3's cumulative usage accounting, conservative upper
bounds, wall/compute/cost and retry limits, with the original accepted start
time. Required owner waiting pauses wall time, while recorded model/compute
usage remains counted. Unknown previous usage prevents additional model work.
No repository source or merge proof is introduced.

The guard runs before original hold consumption, synchronous and outbox model
dispatch, worker admission, provider allowance reservation and actual media/post
POST. Organization then parent/ledger locks protect local checks and are
released before HTTP. If stopping wins after an allowance was committed, its
unknown outcome and original resource identity remain in the journal. Only GET
reconciliation proceeds; the POST is never repeated. Source/evidence readers
remain separate so an inactive goal does not erase recovery authority. Current
key, owner, artifact, approval and original payment checks still apply to reads.

Completed public observations can be refreshed on demand. The exact historical
receipt must validate first. A stale observation requeues the same post in its
provider-confirmed phase, retaining its provider ID, unique slug, create count,
original approval and payment. It performs only exact provider/public reads.
Fresh observations are inert; corrupt history cannot select a different post.
This is HTTP/source-body proof, not browser layout or SEO acceptance.

All 1,156 no-database website tests pass, including 15 new work/recovery
cases. The 86-check focused group passes; model/migration dry-run reports no
changes. The failure audit covers 789 emissions and all 565 test modules are
assigned. Validation covers active original admission, stopped/foreign/refunded/missing
budgets, usage/wall limits, owner waiting, unchanged ordinary CMS work, and
stopping before or between allowance/HTTP and after creation. Public-proof
refresh verifies the same post without a second allowance or approval. All
rollout flags remain off. No local database, provider account, paid model work,
merge or deployment was used. The actual native start/controller and Chat
integration remain the next runtime work.
