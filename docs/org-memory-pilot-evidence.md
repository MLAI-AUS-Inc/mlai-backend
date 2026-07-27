# Admin Brain pilot evidence and exit gates

This runbook covers measurement of an already approved, read-only Admin Roo
pilot. It does not approve a pilot, enable the private query API, widen an
allowlist, or change Public Roo.

The implementation has two deliberately separate inputs:

- an immutable human audit batch for individual answered or abstained queries;
- a pre-approved exit policy that binds the pilot approval hash, fixed window,
  rubric, minimum sample sizes, operational SLOs, and release thresholds.

Both operational files contain sensitive identifiers and belong in restricted
storage outside source control. The checked-in files under `plans/org-memory/`
are draft templates only.

## Freeze the exit policy before the pilot

Copy `plans/org-memory/pilot-exit-policy.template.json` into restricted
operational storage. Before the pilot starts:

1. calculate the canonical SHA-256 hash emitted by
   `check_org_memory_pilot_readiness` and place it in
   `pilot_approval_sha256`;
2. set a window of at least seven complete days;
3. choose a stable rubric version and sample sizes at or above the enforced
   floors;
4. set latency and token ceilings that match the approved SLOs;
5. obtain distinct review, security, and operations approval;
6. change `approval_status` to `approved` only after every value is frozen.

The evaluator rejects a policy approved after its pilot window began, an
expired policy, a changed pilot-approval hash, quality thresholds below the
mandatory release floors, or a sample smaller than the enforced minimum.

## Independent query audit

An official audit may cover only an `answered` or `abstained` query. The
reviewer must:

- be an active member of the same organisation;
- hold an effective `review_claims` capability at review time;
- be someone other than the person who made the query;
- assess every answer decision in the pilot window;
- use the rubric version frozen in the exit policy.

Copy `plans/org-memory/pilot-audit-batch.template.json` and add up to 500
assessments per batch. Do not add questions, answers, citations, names, source
references, Slack IDs, or free-text notes. The database derives the recorded
citation count from the immutable query trace and rejects a mismatched score.
Current-state and temporal fields are valid only for their matching query
modes.

Validate a batch without retaining changes:

```bash
python manage.py import_org_memory_pilot_audits \
  --organization-domain example.org \
  --audit-batch /secure/operations/pilot-audits-001.json
```

After a second person verifies the content-free JSON result, import it:

```bash
python manage.py import_org_memory_pilot_audits \
  --organization-domain example.org \
  --audit-batch /secure/operations/pilot-audits-001.json \
  --apply
```

The import is atomic and idempotent. It stores the batch hash and aggregate
result only; it never returns query IDs, reviewer details, or source content.
An idempotency key reused with a changed batch fails closed.

## Evaluate the completed pilot

After the fixed window closes and all audits are imported:

```bash
python manage.py evaluate_org_memory_pilot \
  --organization-domain example.org \
  --approval-manifest /secure/operations/pilot-approval.json \
  --exit-policy /secure/operations/pilot-exit-policy.json \
  --fail-on-blockers
```

The content-free report blocks exit unless all of the following hold:

- the current pilot approval exactly matches the exit policy hash;
- all traffic came from approved actors and approved private contexts;
- the pilot ran for the approved minimum duration;
- every answered or abstained decision has an independent official audit;
- high-risk citation precision, current-state accuracy, temporal accuracy,
  abstention accuracy, and answer faithfulness meet their approved floors;
- permission leaks and Public/Admin boundary leaks are both zero;
- query failure rate and p95 latency are within policy;
- every answered model query has token usage and p95/daily token totals are
  within policy;
- daily cost ledgers remain under their ceilings for the complete window;
- daily reconciliation and connection health evidence covers the window;
- every source health snapshot is healthy and inside its freshness SLO;
- backfill, deletion, permission-refresh, and revocation work has no failure.

The report contains only hashes, timestamps, aggregate counts, ratios, and
stable status codes. It never emits a query, answer, actor, channel, reviewer,
source, claim, citation, or evidence identifier.

Passing is evidence that the fixed read-only pilot met its pre-approved exit
criteria. It is not authority to add users, sources, actions, publishing, or a
learned selector. Each expansion returns to governance and preflight.

At any suspected leak, disable the private query API and revoke the Admin Roo
credential immediately, while leaving deletion and access-reconciliation work
running as described in `docs/org-memory-pilot-rollout.md`.
