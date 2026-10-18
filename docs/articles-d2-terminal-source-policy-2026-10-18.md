# D2 terminal goals and automatic source hints

The source wake helper treated completed, stopped and needs_attention goals
as reasons to force a new successor with the same target. For a first-article
goal this reaches the canonical new-goal payment path, resetting identity
and budgets after an ordinary push. In the event path, source-scope rebinding
and child cancellation happened before the event's stop policy was checked.

Automatic source changes now reuse only an active, unstopped existing goal.
They never create a successor to a terminal goal. A completed first article
retains its original paid root, consent, proof history and result. A budget
or strategy stop remains inert for new work even if another callback left an active state. A budget-stopped source goal may schedule only its independently bounded original-effect cleanup; its durable budget marker still rejects event/source wakes and positive article activity. See [the stopping continuation](articles-d2-source-stop-2026-10-18.md#cleanup-continues-after-the-new-work-budget-is-spent).
Only the explicit owner-start/retry path may create a new goal. No completed
goal is rewritten to claim that its old proofs verify a different source.

Typed event hints retain the existing successful transient/waiting wake
policy, but that policy runs before any source-scope rebind, child cancellation
or scheduling. The actual source caller respects durable goal ownership even
when rollout/enrollment is disabled, preventing legacy fallthrough from
rewriting a reserved goal. Legacy operations without a goal keep their
existing source/owned-setup-merge behavior. Active source recovery still
retains the same original goal/payment/budgets and causal worker stop.

Four initial regression methods fail on the parent implementation, including
terminal successor creation, stop-marker bypass and mutation before policy.
All rollout flags remain off. No schema, migration, provider/customer work,
paid model, merge or deployment is introduced.

Final validation: 556 strict database/network-denying backend checks pass,
including five new methods and actual source/event/active-recovery paths.
All 605 test modules are selected; 865 production failure emissions are
audited; local immutable parity and read-only model checks pass, with no
model changes. Legacy tests now explicitly permit the additional ownership
read and still require the exact owned-setup update or no update on a stopped
customer push. Full disposable remote CI remains required.
