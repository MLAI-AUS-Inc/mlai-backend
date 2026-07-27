# Organisational-memory extraction

PR10 converts eligible immutable source versions into governed candidate memory. It does not
activate, consolidate, publish, or act on a claim. Those decisions belong to later review and
consolidation stages, so Public Roo remains unchanged.

## Pipeline

1. Source reconciliation schedules one idempotent `EXTRACT` work item per source-version and
   extraction-target fingerprint. Queue payloads contain IDs and version labels, never source text.
2. The worker reloads the current, accessible evidence chunks and applies deterministic source
   safety checks. `NO_AGENT`, revoked, tombstoned, superseded, and inaccessible evidence fails
   closed.
3. Explicit `Decision:`, `Proposal:`, `Commitment:`, `Task:`, and `Fact:` lines produce
   deterministic candidates. A model may add candidates through the OpenAI Responses API using
   strict JSON Schema output. No tools are provided and source content is wrapped as untrusted data.
4. The complete batch is validated before persistence. Every claim requires a bounded exact quote
   in an eligible chunk. Dates must be present in quoted evidence or match the source's known event
   date. Proposals cannot become decisions, and protected-trait inference or negative personality
   judgements are quarantined.
5. Accepted claims are stored as `candidate` with append-only evidence and an initial state event.
   Each claim receives a review item. A durable `no_memory` extraction run prevents repeated model
   work when a source contains no lasting organisational knowledge.

Prompt-injection or credential-like source content is quarantined before a model request. Invalid
schemas, invented citations, prohibited inferences, and proposal/decision confusion are also
quarantined with no claims created.

## Data records

- `MemoryExtractionRun`: immutable outcome, prompt/schema/model/code versions, hashes, usage, and
  safety result. Raw prompts and raw model responses are not stored.
- `MemoryEntity`: governed entity identity. Stable provider references may resolve across sources;
  people without an external reference remain source-version scoped and are never merged by display
  name alone.
- `MemoryClaim`: atomic, bitemporal candidate assertion with epistemic type, confidence,
  classification, and extractor provenance.
- `MemoryEvidence`: exact quote, offsets, original locator, and immutable source/chunk linkage.
- `MemoryClaimLink` and `MemoryClaimStateEvent`: append-only primitives for the later consolidation
  workflow.

## Configuration

```text
ORG_MEMORY_EXTRACTION_MODEL=gpt-5.6-luna
ORG_MEMORY_EXTRACTOR_VERSION=org-memory-extractor-v1
ORG_MEMORY_EXTRACTION_SCHEMA_VERSION=org-memory-extraction-schema-v1
ORG_MEMORY_EXTRACTION_PROMPT_VERSION=org-memory-extraction-prompt-v1
ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS=60000
ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=6000
ORG_MEMORY_EXTRACTION_REASONING_EFFORT=none
```

Changing any model or contract version changes the extraction fingerprint and permits intentional
re-extraction. Code and prompt changes must bump their corresponding versions.

## Offline acceptance suite

```bash
python manage.py evaluate_org_memory_extraction
```

The command runs in CI without credentials or network access. Its seed corpus covers durable
decisions, proposals, noise, prompt injection, credential-like text, proposal/decision confusion,
protected-trait inference, and strict-schema rejection.
