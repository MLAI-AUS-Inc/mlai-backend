# Deliberate public-knowledge publication

PR20 adds an optional, fail-closed bridge from Admin Roo's private
organisational memory to a public-only knowledge corpus. It does not make a
private claim public in place and it does not change Public Roo's existing
behaviour until the new endpoint is explicitly enabled and wired.

## Trust boundary

The public answer path imports no private retrieval, search, selection, or
answering service. It queries only active `PublicKnowledgeItem` rows and their
dedicated full-text/vector fields.

```text
private claim or current summary
→ private MemoryPublication draft
→ deterministic sensitivity scan
→ human redaction confirmation
→ publication review
→ independent authorised approval
→ separate PublicKnowledgeItem snapshot
→ public-only answer endpoint
```

`MemoryPublication` retains the private source relationship, frozen source
fingerprint, redacted proposal, review state, actors, and append-only events.
`PublicKnowledgeItem` contains only the approved public key, revision, title,
body, tags, content hash, public search data, and lifecycle timestamps. It has
no foreign key to a claim, evidence row, source, chunk, summary, reviewer, or
private locator.

## Private publication API

All private endpoints require:

- an Admin Roo service principal allowed on `admin_roo`;
- the `org_memory.publish` service scope;
- a verified, single-use acting-user assertion;
- active organisation membership; and
- the `publish_knowledge` capability.

Endpoints:

```text
GET|POST /api/v1/org-memory/publications
GET|PATCH /api/v1/org-memory/publications/{publication_id}
POST      /api/v1/org-memory/publications/{publication_id}/submit
POST      /api/v1/org-memory/publications/{publication_id}/revoke
POST      /api/v1/org-memory/review-items/{review_id}/resolve
```

Creation and revocation require an `Idempotency-Key`. A candidate may be
generated from an active claim or current ready summary, but it remains a
private draft. Submission requires `confirm_redacted=true`, meaningful
redaction notes, no blocking sensitivity findings, current source/evidence
lineage, and a frozen source fingerprint. Approval reruns every eligibility
and sensitivity check inside the publish transaction.

The safe default requires an approver other than the proposer. Set
`ORG_MEMORY_PUBLICATION_REQUIRE_SEPARATE_REVIEWER=false` only after a documented
governance decision.

## Sensitivity controls

The deterministic scanner blocks payloads containing likely:

- credentials or private keys;
- email addresses;
- phone numbers;
- Slack identifiers;
- internal UUID-style identifiers;
- private Drive, Slack, Notion, or Linear links; or
- financial account identifiers.

The scanner returns finding codes only and never echoes the detected value.
By default, `executive`, `finance`, `people_sensitive`, and `no_agent` source
lineage cannot enter publication at all. This deny list is controlled by
`ORG_MEMORY_PUBLICATION_BLOCKED_CLASSIFICATIONS`.

The scanner is a guardrail, not a substitute for human review. Reviewers must
inspect the exact proposed public payload and its authorised private evidence.

## Public answer API

```text
POST /api/v1/public-brain/answer
{
  "organization_domain": "example.org",
  "query": "When is the community meetup?"
}
```

The endpoint can be used anonymously with `organization_domain`, or by a
service principal that:

- is allowed on `public_roo`; and
- has `public_knowledge.read`.

When a public principal is used, the organisation is taken from the credential
and a supplied domain must match. Requests are throttled by principal or
client address using `ORG_MEMORY_PUBLIC_RATE`. The response either returns a
deterministic answer built only from active published rows and public
citations, or abstains. It never falls back to private memory and never reveals
whether a private candidate or source exists.

PostgreSQL stores a weighted public-only `tsvector` with a GIN index and an
optional 1,536-dimension public-only embedding with an HNSW index. Publication
makes text search live immediately. `store_public_knowledge_embedding` accepts
an already generated, versioned vector for an active public item without
reading private memory. SQLite uses a deterministic text fallback for local
tests.

## Revisions and retirement

Approving another publication with the same `public_key` creates a new
immutable revision and atomically supersedes the former active revision.
Revocation never deletes history; it removes the active item from public
queries immediately.

The system automatically invalidates the private publication and revokes any
active public snapshot when:

- its source access is revoked or tombstoned;
- a new private source version replaces the evidenced version; or
- a linked claim changes lifecycle state.

The public endpoint always filters to `status=active`, so stale, superseded,
revoked, draft, rejected, and invalidated records are not candidates.

## Configuration and rollout

Safe defaults:

```text
ORG_MEMORY_PUBLICATION_ENABLED=false
ORG_MEMORY_PUBLICATION_REQUIRE_SEPARATE_REVIEWER=true
ORG_MEMORY_PUBLICATION_BLOCKED_CLASSIFICATIONS=executive,finance,people_sensitive,no_agent
ORG_MEMORY_PUBLIC_RESULT_LIMIT=5
ORG_MEMORY_PUBLIC_RATE=60/minute
```

Rollout order:

1. Apply migration `0019`.
2. Grant `publish_knowledge` only to named publication reviewers.
3. Add `org_memory.publish` only to the Admin Roo principal.
4. Create or rotate a separate Public Roo principal with
   `public_knowledge.read` and `allowed_surfaces=["public_roo"]`.
5. Exercise draft, rejection, independent approval, revocation, and source
   retirement in staging.
6. Enable `ORG_MEMORY_PUBLICATION_ENABLED`.
7. Wire Public Roo to `POST /api/v1/public-brain/answer`; do not add any private
   fallback.

Rollback is fail-closed: disable `ORG_MEMORY_PUBLICATION_ENABLED`. This stops
candidate mutation and public answers without deleting private audit history
or published revision history.
