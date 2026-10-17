# E5: native per-site setup and first-article controller

The selected owner can start native setup through the existing company website
route's `cms-start-articles` action. It requires the exact current connection,
private publication-policy ID/review choice, a UUID request key and literal
acceptance of the current first-article price. Organization/site locking reserves
one goal and original hold. Repeated or concurrent clicks reuse that goal;
terminal replay never authorizes another debit. All native rollout flags stay
off.

The native scheduler claims one durable lease per site. It reserves the original
read-only capture, requires that exact capture's complete private inventory,
selects an uncovered topic through the canonical approved audience/offer pool,
then persists the choice before dispatch. A coverage race can reselect within a
bounded attempt count before any dispatch exists. Private typed dispatch passes
the original held payment and bounded model budget into the canonical generation
transaction. Every later lease resolves the original outbox first, preserving its
analytics ID, request digest, source and charge through a lost reply or missing
local mirror. Older request digests remain compatible; native model-budget
metadata is sealed and protected across worker snapshots.

The controller reserves only that original run's review/media/stage/approval/post
journal chain. Provider HTTP remains in the existing durable child workers.
Current original run usage is accounted before further work; a contradictory
run-generation report stops. Public proof refresh reads the same post when
needed. The native ledger finishes only when owning private validators supply
all required current proofs. A Git merge is explicitly not required, and provider
acknowledgement or worker ready flags cannot finish it.

Manual final review opens one scoped owner wait. The exact approval wakes that
same goal, and approval arriving before the wait is saved cannot lose its wake.
Required owner waiting pauses wall time without forgiving model usage. A first
transient child failure retries the same phase/journal without restoring a write
allowance; the next occurrence escalates. A changed owner cancels and queues
settlement, a newer lease wins late results, and rollout-off freezes the original
hold. The owner projection exposes milestone/action/status fields without private
provider origins, consent, payer, ledger or credential references.

Validation: all 1,194 no-database website checks pass, including 28 new start,
lease, dispatch, usage, proof-consumption and review-race cases. Models/migration
dry-run reports no changes; 804 failure emissions are audited and all 569 test
modules are assigned. The disposable-CI CMS flow now uses one actual synthetic
hold through original artifact/media/post/public proof, goal completion,
disconnect, retained earned work, unused credit and purge. Database execution is
remote-only and pending CI; no local database, wallet/provider service, paid
model work, merge or deployment ran.

This is not rollout acceptance. Canonical CF resume without a human-action count,
full repair/strategy/fallback integration, terminal successor handling, shared
status/doctor/Chat UI, paid revision lineage, unpublish, hosted fallback and real
provider/browser acceptance remain. Repairable/strategy outcomes currently stop
with recorded native recovery status rather than creating a different paid run.
HTTP/source-body proof does not establish computed browser visibility or SEO.
