# Topic picker research metrics

The founder topic API normalizes both saved discovery options and researched
keyword rows with `content_factory/topic_metrics.py`. This is a read-only
transformation; opening a topic does not start research or charge points.

## Wire contract

- `difficulty` is a finite integer from 0–100 only when its source is
  `dataforseo_labs` or `dataforseo_bulk`. Missing or legacy default scores are
  null. `difficultyStatus` is `available`, `unavailable`, or a supplied provider
  `error`; `difficultyReason` explains missing results. Zero is a valid score.
- `monthlySearches` contains dated provider observations from the latest six
  reported calendar months, sorted chronologically. Google-volume rows contain
  `year`, `month`, `search_volume`; a daily fallback contains `date`, `volume`.
  Missing months stay missing. Legacy numeric arrays without real dates are not
  chart data. Numeric velocity arrays are accepted only with matching real dates.
- `monthlySearchesSource`/`monthlySearchesBasis` and `trendSource`/`trendBasis`
  describe the same series. Google monthly volume takes precedence over a
  conflicting AI-search velocity series. Relative Google Trends observations
  must remain relative interest, not estimated monthly searches.
- `trendStatus` is `breakout`, `rising`, `stable`, `declining`, or `unknown`.
  The first and last equally sized halves are compared (the middle observation
  is skipped for odd counts). At least 100% growth is breakout; over 15% is
  rising; below −15% is declining; other measured changes are stable.
  `trendPercent` is null for a zero baseline; positive demand after a zero
  baseline is breakout. All-zero history, fewer than two observations, or gaps
  between monthly observations produce unknown. No missing values are imputed.
- `trendDescription`, `trendPeriodLabel`, `trendIsEstimated`, and optional
  `trendReason`, `trendCountry`, `trendLanguage`, `metricsCheckedAt` (lookup
  attempt), and `trendLastUpdatedAt` (provider date) accompany the observations.
  Provider-supplied volume is still an estimate of searches; `trendIsEstimated`
  distinguishes synthetic fallback data from directly supplied provider data.

## Persistence and existing records

Bulk keyword sync accepts null/unavailable difficulty without writing null into
the historical non-nullable model column. Updates omit absent or invalid scores
and empty history so a failed provider lookup cannot erase verified evidence.
New unmeasured rows retain the internal model default with an unverified source;
that default is never presented as a measured difficulty.

Empty or unclassifiable velocity observations do not create fake stable
snapshots. Monthly observations remain in the existing keyword JSON column and
the API derives unknown when appropriate. No schema migration is introduced.

When saved run options and keyword rows overlap, the API retains the editorial
title while adding verified research. A newer observed history cannot be replaced
by older stored data after a failed sync. Equal observation dates use known
provider/lookup timestamps, then prefer stored keyword measurements. Geography
and timestamps from a run can enrich the exact same stored observations; changed
series do not inherit unverified provenance. Old records without such metadata
leave it unknown.

Verified difficulty uses comparable known lookup timestamps to choose a newer
score; when either timestamp is unknown, the first verified score is retained.
Search-history dates are not treated as difficulty lookup dates. Search volume
follows the chosen observed trend bundle, including a measured zero; the API
does not substitute a larger historical volume for declining current demand.

This does not backfill production records or guarantee provider coverage for
every keyword. The existing authenticated discovery flow is the research entry
point and retains its current billing/queue rules. A deployment and subsequent
research are needed to populate data absent from historical records. Provider
failure and unavailable data are explicit states, not indefinite “pending”.

## Verification

Run the bounded suite without a database, migrations, credentials or network:

```sh
.venv/bin/python scripts/test_without_database.py content_factory.tests_topic_metrics_unit
```

Tests cover actual extraction/merge and bulk-upsert code with ORM seams plus pure
normalization. Database-backed fixtures in `tests/test_topic_difficulty_metrics.py`
have been updated for dated data but were not run: their harness applies
migrations and requires separate approval. No production lookup or repair ran.
