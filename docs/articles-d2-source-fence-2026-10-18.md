# D2 trusted source stop history

Both trusted source-wakeup paths now record first-article stop history before
ordinary child cancellation. This requires the existing default-off article
recovery, source-refresh, goal, consent, failure-policy and budget enrollment.
The old scope is expected to be stale; eligibility is checked against the
validated rebound, then the complete trusted source-only revision chain is
checked again before recording anything. An owned merge without such an edge
continues through the existing path.

The private `article-source-fence` row binds the original goal and owner, fixed
repository/branch/app root, old and new source scopes, original payment reference,
root and directly owned publication child, exact saved requests, operation
attempts, and pre-cancellation run/operation states. Known execution versions are
retained; unknown versions stay null. An already cancelled/denied original is
never relabelled as automatic source supersession. No new charge, generation,
model, Git or worker operation is reserved by this record.

The row's completed state means the backend history record is committed. Its
receipt explicitly says `remote_confirmed: false` and `effect_authority: false`.
It is not remote quiet, a worker read grant, a durable content seal, approval,
merge/live proof, or permission to create a replacement. Remote fencing and
original accounting must be resolved before new effects.

## Permanent old-operation fence

Every recorded workflow gets a private fence pointer. Generic operation/token,
resume, setup, publication, merge and cleanup paths refuse that operation even
when it is completed and the rollout flag is subsequently disabled. Later work
must reserve a separate current execution operation. Source guards, ordinary
owner-cancel guards and promotion snapshots are unchanged.

The rebound is saved inside the existing site transaction before cancellation.
Only a server-only `source_fence` function argument can classify that cancellation
as the exact automatic source stop. A client field with the same name is ignored.
The recorded stop can fence a completed shared operation whose publication child
is still running. Cancellation/acknowledgement is limited to the recorded
operation attempt and exact root/child request digest; no foreign child is added.

An explicit owner stop of a fenced article follows the normal whole-goal stop,
revokes the source-recovery record, and queues existing cleanup and original
refund settlement. A suspended budget goal can still be stopped. No recovery can
undo this later owner action. Automatic cancellation keeps the original goal
and its one debit; it does not reserve a refund or a successor article.
The ordinary draft `cancel` and `deny` controls also take this path, including
after flag rollback; they cannot silently erase the automatic-stop cause while
leaving its original goal eligible to refresh. Their marker lookup is scoped to
the run's recorded connection/generation and the current company, then rechecked
under authority. Existing draft controls without a fence retain their path.
A second lookup after legacy worker HTTP closes the race where source
supersession is recorded while the owner control is in flight. Later review
metadata cannot prevent an owner stop of the exact recorded run/operation
attempt; historical reads still require the complete saved-request digest.

## Historical metadata and remaining worker integration

The canonical first-article lookup still proves the original tenant, dispatch,
goal and payment-owned root. Only its generic operation lookup is replaced, for
this privately fenced root, by fresh owner/company context and canonical
original-spend/refund checks. The current goal, exact private record, original
request digest and unchanged operation attempt are rechecked under authority.
This returns historical metadata without modifying status, request or result,
granting any token, carrying approval, or treating old content as a current proof.

The existing v1 worker inspection cannot read cancelled artifacts. A cancelled
fenced root or an unknown pre-stop execution version therefore waits with a
transient code before any inspection dispatch. The next paired contract must
confirm worker fencing and original usage, then grant only the exact historical
artifact read. It must not enable ordinary cancellation bypass or resume. Existing
terminal roots can still be inspected through the separately reserved private
read operation; those observations remain free of execution authority.

Remaining D2 work includes that worker fence/read purpose and old credential
revocation; exact native auto-merge and uncertain/merged-effect reconciliation;
rechecked editorial sealing; separate current execution/delivery; fresh
build/render/review/merge/live lineage; and article-specific strategy escalation.
Deeper owned editorial revisions and source events against terminal/budget-stopped
first-article goals also need end-to-end audit before rollout. This change does
not establish D2 completion or the nightly one-shot target.

## Validation and rollback

Strict database/network-denying tests run both actual source-wakeup functions,
the real cancellation path, source/private metadata validation and canonical
billing with persistence/provider seams. They cover pre-stop history, missing
source edges, changed owner/goal/request/attempt, an earlier owner cancellation,
unknown versions, flag rollback, completed shared-operation publication children,
exact root/child cancellation receipts, missing original payment/refund proof,
and later owner whole-goal stop. No schema or migration is introduced.
The combined related/control selection passes 440 tests, including 19 source-fence
cases and activation, approval, setup merge/failure, environment and hosted-build
controls. CI fully selects all 603 test modules. The offline failure audit covers 860
production emissions, immutable inspection/transport parity passes, and model
checks report no changes. Remote provenance and full disposable CI remain required.

Keep the source-refresh and recovery flags false. Once a fence is recorded, flag
rollback never restores old operation rights; its private history is retained.
No production/customer, paid model, hosting, merge or deployment operation is part
of this work. Remote full-suite CI remains required.
