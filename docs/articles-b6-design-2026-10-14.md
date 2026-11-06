# B6 relevant-event goal wake-ups

14 October 2026. The owner authorized continuous implementation. This branch
uses the reviewed B2/B3/B4 stack without merging or releasing those dependencies.
The runbook's requested branch is `feat/goal-wakeups`; all wake flags default off.

Verified GitHub events may wake current work, never prove an approval, merge,
CI pass or live deployment. Match immutable installation/repository IDs and the
selected branch/source or an existing bound setup PR. Added repository access,
merged/closed PR, completed check-run/check-suite and deployment-status signals
are bounded and typed. Old generation, foreign PR/source and malformed events
cannot wake a replacement or resurrect cancelled, denied or deleted work.

Persist the due time under the connection/organization authority lock and queue
an immediate pass after commit through a bounded local executor and cache lock.
Jitter absorbs webhook bursts; the ordinary durable scheduler remains the fallback
if a web process exits. A running claim keeps its lease/state: event delivery must
not create a concurrent second pass. Rechecks preserve B4 counters and cannot
reopen deterministic needs-attention or exhausted/malformed budgets. A trusted
new provider-success signal may request a read-only verification recovery when
that policy permits it; no worker or webhook grants new repository authority.

Source-only configuration revisions require an explicit trusted transition. The
source-reverify record must retain previous/current configuration revisions;
identity changes remain separate authority changes. Rebind an active goal only
for the recorded source transition, invalidate source proofs, preserve budgets,
record the scope transition and fence its old child. Terminal goals retain history
and use a deterministic successor. This fixes the gap where a push incremented
configuration revision before the goal could accept the new source scope.

Tests cover event × source/branch/PR/installation/generation, duplicate delivery,
per-connection lock, durable fallback, active-lease preservation, policy-controlled
recovery and source-revision races. No production webhook or worker is invoked.
