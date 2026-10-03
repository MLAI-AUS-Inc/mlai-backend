# Website baseline metric presentation

The baseline API distinguishes vendor measurements from older estimates.
Snapshots remain in the existing JSON fields; no migration or backfill is needed.

[`baseline_metrics.py`](../content_factory/baseline_metrics.py) normalizes both
compact and full responses. `authority` requires Ahrefs provenance and a numeric
`domainRating` on the 0–100 scale. The current method is `ahrefs-dr-v1`.
Documented older Ahrefs results retain their value; other old authority estimates
are unavailable and excluded from the overall score.

`aiVisibility` uses `ai-mentions-v2`. Its percentage is the count of AI answers
mentioning the brand divided by the count of valid answers. Citation counts are
separate. Legacy weighted mention/citation scores are unavailable, not zero.
The worker samples unbranded topics and excludes failed or empty answers.

Compact responses retain `source`, `methodVersion`, `domainRating`,
`measurementDate`, `responseCount`, `mentionCount`, `citationCount`,
`requestedCount`, `queryCount`, `providerCount`, `requestedProviderCount`,
`countryCode`, and `promptSetId`, plus the relevant per-provider counts and model.
Missing scores are null; `legacy_method` identifies measurements requiring a
fresh scan. Score weights and denominators do not change when answer evidence is
included.

## AI answer evidence

Compact responses now preserve bounded `metrics.aiVisibility.providers[].prompts`
from the existing snapshot JSON. Each row contains its topic `query`, actual
`prompt`, and explicit `status`. Successful rows include boolean `mentioned`
and `cited`, `citedUrls` for the startup's site, and up to two short
`mentionContexts`. Failed or unavailable rows retain their status and bounded
error text; they do not acquire false mention/link flags. Malformed measured
flags produce `unavailable` with `reasonCode: invalid_answer_evidence`.

Existing snapshots can expose `responseExcerpt` without claiming to contain a
full answer. If no usable prompt records were saved, the provider's `prompts`
key remains absent so clients can show a request to rerun the scan. New worker
records can also supply `answerText`, `answerTextTruncated`, all `sourceUrls`,
the provider-returned `modelName`, a timezone-aware `capturedAt`, and nullable
`webSearchUsed`.

`measurementType: model_probe` and `evidenceVersion: llm-final-answer-v1` distinguish
these model responses from observed consumer search interfaces. `countryCode`
is nullable and describes the provider's applied request market;
`requestedCountryCode` describes the intended market. `locationTargeting` is
`requested`, `unsupported`, or `unknown`. The API preserves explicit nulls and
never infers geography or web search from missing historical metadata.

[`baseline_ai_evidence.py`](../content_factory/baseline_ai_evidence.py) validates
these projections. It keeps four supported provider keys, at most 40 prompt
rows per provider, 500 characters per query, 2,000 per prompt, 320 per excerpt,
16,000 per answer, two 280-character mention contexts, ten startup citation
URLs, and twenty source URLs. Answer truncation remains explicit even if the
worker truncated a shorter body. Clickable URLs must use HTTP(S), have a valid
host/port, and contain neither credentials nor whitespace/control characters.
Malformed arrays and untyped values are excluded rather than coerced.

These fields use existing `WebsiteBaselineSnapshot.metrics` and `raw_payload`
JSON columns. No database migration, backfill, provider request, or rescan is
triggered by reading them. Full serialization and saved snapshots are unchanged.

Overall score, coverage, and summary are derived from eligible measurements on
read. History returns null for incompatible old scores, `metricMethods` for
measurement versions, and `metricContexts.aiVisibility` for the prompt set,
country, and provider coverage. Clients must avoid joining incompatible history.

Contract tests run without a database, migrations, or network:

```sh
.venv/bin/python scripts/test_without_database.py \
  content_factory.tests_baseline_metrics_unit \
  content_factory.tests_baseline_ai_evidence_unit
```
