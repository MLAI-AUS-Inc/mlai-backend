# Admin Roo retrieval and grounded answers

PR12 adds the private, read-only query plane for Admin Roo. Public Roo remains
unchanged. The query API is fail-closed behind `ORG_MEMORY_QUERY_API_ENABLED`
and still requires the existing organisation-scoped service credential, signed
actor assertion, active membership, and actor capability checks.

## Retrieval boundary

Every request is planned into an intent, optional entity scope, time range, and
claim kinds. The selector then:

1. resolves the allowed memory classifications from the verified actor;
2. removes cross-organisation, revoked, tombstoned, inaccessible, and
   `no_agent` records before any rank calculation;
3. runs structured-current-state, claim-text, source-text, and optional vector
   lanes over that authorised set;
4. fuses ranks deterministically, then applies bounded authority, confidence,
   current-state, status, and entity adjustments;
5. measures evidence sufficiency and packs a de-duplicated evidence bundle
   within the configured token budget.

The selector does not use an LLM reranker. Candidate traces contain IDs,
scores, lane ranks, and feature flags but no unselected evidence text. Raw
queries are hashed and stored only after credential-like strings are redacted.
Prompt, schema, selector, answerer, embedding, and model versions are recorded
on every query log.

## Grounded-answer boundary

Only the packed authorised bundle reaches the answer model. The default is
`gpt-5.6-terra`, Responses API structured output, reasoning effort `none`, no
tools, and `store=false`. Evidence is explicitly marked as untrusted data. The
application rejects extra response fields, duplicate citation IDs, citations
outside the selected bundle, and answers with no authorised citations.

Insufficient evidence does not call the model. It returns exactly:

> I do not have enough authorised evidence to answer that reliably.

Partial, stale, conflicted, semantically degraded, and unhealthy-source states
are returned as explicit warnings rather than hidden. An absence of evidence
is not converted into a factual “no”. The model cannot mutate memory or call
connectors; corrections enter the existing human review flow as proposals.

## Private API

All routes use `/api/v1/org-memory/`:

- `POST answer` — retrieve and return one cited grounded answer;
- `POST search` — return the selected evidence pack without answer generation;
- `GET entities/<entity-id>/timeline` — return an ACL-filtered claim history;
- `GET queries/<query-id>/trace` — return the requester's redacted trace, or a
  reviewer-visible trace when the actor has `review_claims`;
- `POST feedback` — record relevance/correctness/staleness feedback and create
  a correction proposal when incorrect feedback supplies a selected claim and
  correction text.

Example answer request:

```json
{
  "query": "What is the current status of the Pilot project?",
  "answer_mode": "auto",
  "as_of": null,
  "time_range": {"start": null, "end": null},
  "max_context_tokens": 6000
}
```

The organisation, Slack channel, thread, and user are taken from the verified
actor assertion. Body values may only repeat those values; they cannot override
them.

## Configuration and rollout

```text
ORG_MEMORY_QUERY_API_ENABLED=false
ORG_MEMORY_SELECTOR_VERSION=org-memory-rules-selector-v1
ORG_MEMORY_QUERY_CANDIDATE_LIMIT=100
ORG_MEMORY_QUERY_RESULT_LIMIT=20
ORG_MEMORY_QUERY_VECTOR_ENABLED=true
ORG_MEMORY_ANSWER_MODEL=gpt-5.6-terra
ORG_MEMORY_ANSWERER_VERSION=org-memory-answerer-v1
ORG_MEMORY_ANSWER_SCHEMA_VERSION=org-memory-answer-schema-v1
ORG_MEMORY_ANSWER_PROMPT_VERSION=org-memory-answer-prompt-v1
ORG_MEMORY_ANSWER_MAX_OUTPUT_TOKENS=1600
ORG_MEMORY_ANSWER_REASONING_EFFORT=none
ORG_MEMORY_ANSWER_MAX_CONTEXT_TOKENS=6000
```

Before enabling a pilot organisation:

1. apply migration `org_memory.0012` and leave the feature flag off;
2. run `python manage.py evaluate_org_memory_retrieval`;
3. run the full `org_memory.tests` package and the PostgreSQL search check;
4. inspect source health, extraction/consolidation queues, and permission
   refresh freshness;
5. enable the flag for the Admin Roo backend deployment and test with a
   read-only service principal plus representative actors;
6. review abstention, citation, leakage, stale-warning, latency, and feedback
   rates before broadening access.

Rollback is immediate: turn `ORG_MEMORY_QUERY_API_ENABLED` off. This makes the
five query routes return 503 while ingestion, permission refresh, extraction,
consolidation, and Public Roo continue unchanged. Retain query and feedback logs
for audit. Reverse migration `0012` only if those records are no longer needed.

CI runs the offline retrieval seed suite along with migration drift, Django
checks, all organisational-memory tests, and the PostgreSQL vector/FTS checks.

Future learned-ranking experiments are isolated from this request path. See
`docs/org-memory-selector-shadow.md` for the disabled-by-default,
currently-authorised, pseudonymised export and offline shadow-evaluation
boundary.
