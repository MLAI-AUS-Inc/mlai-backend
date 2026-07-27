# Organisational memory: review, summaries, and digests

PR19 adds the read-only operating surface that sits after extraction,
consolidation, and daily reconciliation. It does not enable a provider, deploy
Admin Roo, publish public knowledge, or permit an agent to change a source
system.

## Review dashboard

All review routes require a verified Admin Roo service principal with
`org_memory.read` and an acting user with `review_claims`:

- `GET /api/v1/org-memory/review-dashboard`
- `GET /api/v1/org-memory/reviews`
- `GET /api/v1/org-memory/reviews/<review-id>`
- `GET /api/v1/org-memory/reviews/<review-id>/evidence`
- `POST /api/v1/org-memory/review-items/<review-id>/resolve`

The dashboard exposes contradiction, correction, entity-merge, sensitivity,
and stale queues. Each queue reports open count, high-priority count, overdue
count, and oldest item. It also reports the latest unhealthy connection
snapshots without source content.

Review lists are content-minimised. A detail response returns the review reason,
claim statement, exact evidence quote, source/version/chunk identifiers, source
locator, and canonical URL only when the acting user can view that evidence
classification and the source ACL remains active. Quarantined extraction
reviews may expose bounded source chunks under the same checks. `no_agent`
content is never returned.

Staleness detection now opens one idempotent stale review per claim/stale
deadline. Contradiction, correction, sensitivity, and entity queues continue to
use the existing governed review records and consolidation workflows.

Review resolution requires `confirm=true` and a valid `Idempotency-Key`.
Contradictions must select an evidenced winner; corrections must reference an
independently evidenced replacement; entity merge/split requests validate both
entities; and stale claims may be acknowledged or retracted. The endpoint
delegates to the existing consolidation/correction/entity state machines so
claim state events, current-state refresh, review attribution, and original
evidence history remain intact. Replaying the same key returns the completed
review without applying it twice. A different key cannot overwrite a terminal
review.

### Reprocess control

`POST /api/v1/org-memory/reviews/<review-id>/reprocess` additionally requires
`source.manage`, `manage_sources`, and `confirm=true`.
Pass `source_id` when the review has more than one evidence source. The source
must be authorised evidence for that review and belong to a selected,
reprocessable scope. The endpoint creates the existing audited `reprocess`
action for exactly that scope; it does not run provider work in the web
request. A supplied `Idempotency-Key` is used directly; otherwise the
review/source pair supplies a deterministic key.

## Deterministic summaries

After a fully successful daily reconciliation, the scheduler builds:

- one day summary for claims observed or recorded that day;
- one calendar week-to-date summary;
- one current summary for each active project entity;
- one summary for each eligible Slack/Gmail thread touched that day.

Summary text is a deterministic ordered list of active claims. No model call is
made. `ORG_MEMORY_SUMMARY_MAX_CLAIMS` bounds each artifact. Every summary stores:

- explicit ordered `MemorySummaryClaim` rows;
- every currently eligible `MemoryEvidence` row for those claims;
- source/version/chunk lineage through the evidence relation;
- the originating daily report, time window, content fingerprint, parent
  summary, and required classification set.

Thread summaries point to their day summary. Regeneration is idempotent for a
daily report. A later generation marks the prior subject/type summary stale.

Read routes are:

- `GET /api/v1/org-memory/summaries`
- `GET /api/v1/org-memory/summaries/<summary-id>`

The detail route includes complete claim and evidence lineage. The whole
artifact is hidden unless the acting user has every classification capability
required by its claims.

## Reconciliation-gated digests

The daily open-loop digest selects active commitments, tasks, open loops,
questions, and risks. The weekly committee digest selects decisions,
commitments, project status, risks, opportunities, metrics, and events from the
previous complete week. Weekly generation runs on
`ORG_MEMORY_WEEKLY_DIGEST_WEEKDAY` (`0` is Monday).

`ORG_MEMORY_DIGEST_MAX_ITEMS` bounds both digests. Every item has a direct claim
link, all eligible evidence links, and the current project-summary link where
available.

A report is successful only when it is `completed`, has at least one connection
snapshot, and every snapshot is healthy with a completed/no-op schedule. If
that invariant fails:

- no summaries are created;
- the digest is stored as `blocked`;
- it contains no claim/source content or items;
- it names affected provider/configuration health states only.

Read routes are:

- `GET /api/v1/org-memory/digests`
- `GET /api/v1/org-memory/digests/<digest-id>`

Blocked digests remain visible as content-free operator warnings. Ready digests
require every classification capability represented by their items.

## Permission and deletion behaviour

Source access revocation, tombstoning, and an inaccessible newly captured ACL
immediately:

1. mark every linked current summary stale and non-current;
2. replace affected digest content with a content-free blocked warning;
3. leave immutable source, claim, review, and evidence audit history intact.

A claim lifecycle change also invalidates linked current summaries and replaces
affected digest text with a content-free blocked state. This prevents a
corrected, retracted, superseded, or contradicted claim from surviving in a
derived surface until the next daily run.

The read APIs also re-check live source lifecycle, source-version tombstones,
and ACL snapshots. This is a second fail-closed guard if an invalidation job is
interrupted.

## Rollout and rollback

Keep `ORG_MEMORY_QUERY_API_ENABLED=false` until Admin Roo identity, capability,
and source-scope tests pass for the pilot organisation. This PR makes no
provider calls during migration and does not change `ORG_MEMORY_ENABLED_PROVIDERS`.

To stop serving the new surfaces, disable `ORG_MEMORY_QUERY_API_ENABLED`.
Daily source reconciliation continues. To stop generation while preserving
data, deploy the prior scheduler code without reversing the migration. Reverse
`org_memory.0018_review_summaries_digests` only after retaining any required
summary/digest audit export; reversal deletes derived summaries, digests, and
their lineage rows, not canonical claims or source evidence.
