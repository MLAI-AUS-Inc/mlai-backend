# Organisational-memory temporal consolidation

PR11 turns reviewed extraction candidates into a governed, time-aware memory
graph for Admin Roo. It does not grant Public Roo access, publish content, or
permit a model to perform state changes directly.

## Decision boundary

`org_memory.consolidation` first applies deterministic structural rules and
only sends genuinely ambiguous cases to a bounded consolidation model. The
strict response contains one proposed operation:

- `NEW`
- `DUPLICATE`
- `SUPPORTS`
- `REFINES`
- `SUPERSEDES`
- `CONTRADICTS`
- `IGNORE`

The model receives a candidate plus a bounded set of structurally compatible
claims. It has no tools, cannot nominate an ID outside that set, and cannot
apply its proposal. Application code validates the operation and owns links,
state transitions, reviews, projections, and audit events. Exact duplicates
and independent supporting evidence can consolidate automatically. `NEW` and
`REFINES` respect the candidate review requirement; `SUPERSEDES` and
`CONTRADICTS` always require human review.

The default is `gpt-5.6-luna` with reasoning effort `none`, strict Responses
API structured output, versioned prompt/schema/consolidator identifiers, and
no stored provider response body. The defaults follow OpenAI's current
extraction/classification tier guidance. Changing any version changes the
idempotency fingerprint and makes intentional reprocessing observable.

## Temporal truth and history

Claims follow an explicit legal transition table. Every transition appends a
`MemoryClaimStateEvent`; evidence text and assertion fields remain immutable.
Activation establishes `valid_from` when extraction did not supply it.
Supersession closes the previous validity interval and preserves both claims.

`eligible_claims_as_of()` supports current and historical-as-of reads:

- current reads admit active or stale claims inside their validity interval;
- historical reads may also admit superseded or contradicted claims inside the
  interval where they were believed valid;
- inaccessible, revoked, tombstoned, or `no_agent` evidence is excluded before
  a claim becomes eligible;
- `known_at` can additionally bound results by recording time.

`MemoryCurrentState` is a rebuildable projection keyed by organisation,
canonical entity scope, claim kind, and predicate. It stores the selected
claim, a bounded value, validity time, independent-source count, and explicit
`stale` or `unresolved_conflict` warnings. Source access revocation and
tombstoning refresh the projection transactionally, so inaccessible evidence
does not linger as current state.

## Staleness defaults

Staleness is a warning and lifecycle state, not deletion:

| Claim kind | Default age |
| --- | ---: |
| task, open loop | 14 days |
| project status | 30 days |
| relationship | 90 days |
| procedure | 180 days |
| person profile | 365 days |
| decision, policy, lesson, event | no automatic expiry |

A source policy may override these defaults. The daily scheduler marks due
active claims stale and refreshes affected projections.

## Entity resolution and evidence lineage

Entity merge and split operations append `MemoryEntityResolutionEvent` rows.
People are never merged by display name alone: they need a shared stable
external reference or an approved review. The duplicate entity remains as an
alias pointing to the canonical entity, so claim history is not rewritten.

Corroboration counts evidence lineages, not raw files or rows. Repeated versions
of one source and Google Drive artifacts identified as copies share one lineage.
`DUPLICATE` and `SUPPORTS` copy only independently sourced evidence into the
surviving claim.

## Corrections and conflicts

A correction creates a `MemoryCorrectionProposal` and a high-severity review.
It can only be applied with a different, independently evidenced replacement
claim. Approval activates the replacement, supersedes the original at the
replacement's effective time, retains both evidence sets, and adds a
`SUPERSEDES` link.

Contradictions retain both claims and set `unresolved_conflict` on the current
projection until a reviewer selects one of the two claims. Resolution marks the
loser contradicted without deleting it.

## Runtime and operator checks

Extraction schedules identifier-only `CONSOLIDATE` work. Replay is idempotent
for the claim plus model/prompt/schema/consolidator fingerprint. The standard
memory worker executes it; source text is never copied into the queue payload.

Run the offline contract and policy suite with:

```bash
python manage.py evaluate_org_memory_consolidation
```

CI runs that seed suite, migration drift checks, Django checks, and the complete
`org_memory.tests` package. Django admin exposes consolidation runs, entity
resolution history, correction proposals, current-state warnings, claims,
evidence, and state events as operational records.

## Configuration

```text
ORG_MEMORY_CONSOLIDATION_MODEL=gpt-5.6-luna
ORG_MEMORY_CONSOLIDATOR_VERSION=org-memory-consolidator-v1
ORG_MEMORY_CONSOLIDATION_SCHEMA_VERSION=org-memory-consolidation-schema-v1
ORG_MEMORY_CONSOLIDATION_PROMPT_VERSION=org-memory-consolidation-prompt-v1
ORG_MEMORY_CONSOLIDATION_MAX_MATCHES=20
ORG_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=1200
ORG_MEMORY_CONSOLIDATION_REASONING_EFFORT=none
```
