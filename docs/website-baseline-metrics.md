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
Prompt transcripts remain excluded. Missing scores are null; `legacy_method`
identifies measurements requiring a fresh scan.

Overall score, coverage, and summary are derived from eligible measurements on
read. History returns null for incompatible old scores, `metricMethods` for
measurement versions, and `metricContexts.aiVisibility` for the prompt set,
country, and provider coverage. Clients must avoid joining incompatible history.

Contract tests run without a database, migrations, or network:

```sh
.venv/bin/python scripts/test_without_database.py content_factory.tests_baseline_metrics_unit
```
