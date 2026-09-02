# Learned selector export

This document covers the offline selector dataset export — an experiment
boundary for a future learned organisational-memory selector. It does not add
a learned selector to request handling. The deterministic selector in
`org_memory.retrieval.select_memory` remains the only production ranker, and
neither retrieval nor answer generation imports the export module.

The shadow evaluation subsystem that once accompanied the export (the
`evaluate_org_memory_selector_shadow` command, the `LearnedMemorySelectorV2`
artifact parser, and the `MemorySelectorShadowRun`/`MemorySelectorShadowResult`
tables) was removed in the 2026-09 database cleanup after the evaluation
programme ended. Only the export path below remains.

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
ORG_MEMORY_SELECTOR_EXPORT_SECRET=
ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES=3000
ORG_MEMORY_SELECTOR_SHADOW_LIMIT=10000
```

Use a dedicated random export secret containing at least 32 bytes. Do not reuse
the Django, connector-encryption, service-principal, Slack signing, or webhook
secret. Export is off by default behind its own kill switch.
`ORG_MEMORY_SELECTOR_SHADOW_LIMIT` caps how many query traces a single dataset
build reads; `ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES` is also read by the pilot
readiness report as the labelled-trace evidence gate.

## Export command

Deliberately enable export in an offline worker environment:

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

## Operations and rollback

Rollback is immediate: set `ORG_MEMORY_SELECTOR_EXPORT_ENABLED` to false.
Production answers are unaffected because the production request path never
uses the export module. Remove exported files from their restricted offline
storage according to the approved retention schedule.

Reinforcement learning is not implemented or enabled. Do not add online
learning from user interactions: feedback remains evidence for reviewed
offline evaluation only.
