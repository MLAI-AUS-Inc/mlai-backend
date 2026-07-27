# Learned selector export and shadow evaluation

PR22 adds an offline experiment boundary for a future learned organisational-
memory selector. It does not add a learned selector to request handling. The
deterministic selector in `org_memory.retrieval.select_memory` remains the only
production ranker, and neither retrieval nor answer generation imports the
shadow module.

## Security and privacy boundary

Every export rebuilds authorisation at export time rather than trusting the
historical trace:

1. resolve the query requester's currently active, verified organisation
   identity and membership;
2. resolve current capability grants and allowed classifications;
3. parse every candidate reference and require that it still belongs to the
   selected organisation;
4. require active source lifecycle, live source access, a non-tombstoned
   version, an accessible and non-revoked ACL snapshot, and an
   active-for-retrieval chunk or currently evidenced claim;
5. exclude the entire trace if any candidate is malformed, missing, revoked,
   cross-organisation, or no longer allowed.

The exported JSON contains no query, answer, correction text, actor/user/Slack
identifier, channel, request ID, source/citation locator, evidence text,
statement, claim ID, chunk ID, or database ID. Organisation, query, and
candidate references are HMAC-SHA-256 pseudonyms generated with a dedicated
export secret. Rotate that secret to make separately exported datasets
unlinkable.

Only a fixed, versioned numeric feature allowlist is exported. Unknown trace
fields are dropped. The export uses explicit claim-level feedback:

- `relevant` and `correct` are positive;
- `irrelevant`, `incorrect`, `stale`, and `harmful` are negative;
- `missing` remains query-level feedback and is not attached to an existing
  candidate;
- conflicting labels are omitted;
- no unlabelled candidate is treated as an implicit negative;
- pairwise examples exist only where a trace contains both an explicit
  positive and explicit negative.

Exports are written atomically with mode `0600`. They are still sensitive
derived data: keep them out of source control, restrict storage and retention,
and delete them under the organisation's reviewed data-deletion policy.

## Feature flags

```text
ORG_MEMORY_SELECTOR_EXPORT_ENABLED=false
ORG_MEMORY_SELECTOR_SHADOW_ENABLED=false
ORG_MEMORY_SELECTOR_EXPORT_SECRET=
ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES=3000
ORG_MEMORY_SELECTOR_SHADOW_LIMIT=10000
ORG_MEMORY_SELECTOR_MIN_NDCG_GAIN=0.02
ORG_MEMORY_SELECTOR_ARTIFACT_MAX_BYTES=262144
```

Use a dedicated random export secret containing at least 32 bytes. Do not reuse
the Django, connector-encryption, service-principal, Slack signing, or webhook
secret. Export and shadow evaluation have separate kill switches and both are
off by default.

## Export command

After applying `org_memory.0021_memory_selector_shadow`, deliberately enable
export in an offline worker environment:

```bash
python manage.py export_org_memory_selector_data \
  --organization-domain example.org \
  --output /secure/offline/example-selector-v1.json
```

The command refuses to overwrite an existing file unless `--overwrite` is
explicitly supplied. Its console output contains only the destination, dataset
hash, and aggregate eligible/labeled/excluded counts.

Review `manifest.excluded_counts` before training. A high exclusion rate is a
permission/identity/data-quality signal, not a reason to weaken the filter.
The dataset hash covers the schema, manifest, and all records and is stable for
unchanged inputs and the same pseudonymisation secret.

## LearnedMemorySelectorV2 artifact

Shadow evaluation accepts only a small local JSON linear-scoring artifact. The
schema is exact: unknown fields, unknown features, non-finite or out-of-range
weights, unsupported interface/schema versions, invalid version names, and
oversized artifacts are rejected. The evaluator performs no network calls,
loads no pickle or executable model format, invokes no LLM, and exposes no
tools.

Example:

```json
{
  "interface_version": "learned-memory-selector-v2",
  "version": "offline-linear-2026-08-01",
  "feature_schema_version": "org-memory-selector-features-v1",
  "bias": 0.0,
  "weights": {
    "baseline_score": 0.7,
    "lexical_relevance": 0.2,
    "current_state": 0.1
  }
}
```

The model may rank only candidates already admitted by deterministic current
access and lifecycle filters. There is no interface through which it can add a
candidate.

## Shadow command and release gate

```bash
python manage.py evaluate_org_memory_selector_shadow \
  --organization-domain example.org \
  --artifact /secure/offline/offline-linear-2026-08-01.json
```

With fewer than `ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES` eligible labelled
traces, the run is persisted as `blocked`, scoring is not invoked, and no
per-query result rows are created. The default minimum is 3,000 representative
traces.

Eligible runs persist content-minimised comparison rows and aggregate:

- top-k overlap and disagreement rate;
- baseline and shadow NDCG;
- NDCG gain;
- baseline and shadow explicit-pair accuracy;
- artifact, dataset, feature-schema, and selector versions.

`promotion_eligible` is only an offline signal. It requires the labelled-trace
gate, the configured NDCG improvement, and no pairwise regression. It never
changes the production selector. A release would require a later reviewed
implementation, the full gold/security suite, reward-hacking analysis,
latency/cost review, and a separate production kill switch.

Reinforcement learning is not implemented or enabled. Do not add online
learning from user interactions: feedback remains evidence for reviewed
offline evaluation only.

## Operations and rollback

A scheduler may run the shadow command daily after the label threshold is met.
Repeated identical dataset/artifact runs are idempotent. Inspect runs and
content-free results in Django admin.

Rollback is immediate: set both selector flags to false. Production answers are
unaffected because the production request path never uses the shadow module.
Retain run metrics according to audit policy; remove exported files from their
restricted offline storage according to the approved retention schedule.
