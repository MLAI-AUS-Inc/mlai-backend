# CRM audience profile mappings

The audience builder reads typed facts from the protected `Contact.attributes.audience_profile_v1` namespace. Generic contact attributes remain scalar. A `gender` or `career_stage` key in an ordinary import is not automatically a trusted audience fact.

`crm/audience_taxonomy.py` defines stable city, role, stage, transition, gender, age-band and interest IDs. `crm/audience_profiles.py` validates the envelope, provenance and source dates. `crm/audience_sources.py` maps reviewed source records and resolves updates. These modules do not change consent, review holds, engagement scores or delivery state.

## Facts and missing values

Facets have a `status` of `known`, `not_provided`, `withheld`, `conflicted` or `unmapped`, a bounded list of stable `values`, a derived `value_count` and bounded `evidence`. Evidence records contain `provenance`, `source_kind`, `source_record_id`, `observed_at`, canonical values and a `self_report_confirmed` attestation. Raw answers remain in the owning source, not duplicated into the projection.

Gender and age require explicit self-report evidence; a correction can attest a verified existing self-report. Career stage requires reported or verified evidence. Roles may use a reviewed deterministic title mapping. Names, photographs, pronouns, age and founder status never imply a gender or career stage. Adult eligibility is not an age band. Self-described gender uses a category only; raw free text is not exposed as a catalogue choice.

City connections and reported city are separate facets. Neither is labelled residence. Multi-city and multi-role facts are supported. An ordinary negative demographic predicate requires known usable values. Explicit set subtraction may retain people whose excluded trait is unknown; it does not establish the opposite trait.

Updates to the same source record supersede its earlier observation; older replays are ignored. Equal-authority conflicting current single-value sources yield `conflicted`. Lower-authority imports fill gaps without replacing reported/verified choices. Retraction removes the named source's contribution. Admin correction explicitly resolves one facet under a contact row lock.

## API

All paths below use `/api/v1/community-chat/crm/` and inherit the installation, MLAI admin, own-community and CRM feature gates.

- Contact detail returns `audience_profile` separately from ordinary `attributes`.
- `GET contacts/{id}/audience-profile/` returns `{audience_profile: ...}`.
- `POST contacts/{id}/audience-profile/` corrects one facet. Body: `facet`, `status`, `values`, `source_record_id`, `observed_at`, `provenance`, and optional `self_report_confirmed`. The service fixes source kind to `admin_correction`. Gender/age require `self_report_confirmed: true`; a selected chip alone is insufficient. Invalid inputs return `invalid_audience_profile` with a pointer and reason.
- `POST contacts/{id}/audience-profile/preview/` accepts `{mappings: [...], records: [...]}` and returns `{audience_profile, counts, applied: false}`. It previews explicit mappings without writing anything.
- `POST contacts/{id}/audience-profile/import/` explicitly applies that reviewed mapping to one contact under a row lock and returns the recomputed profile/counts with `applied: true`. The Audience details drawer exposes preview before this separate apply action. Source provenance stays attached, and messaging consent is unchanged.
- The segment catalogue exposes `facets` with capability status, known-count, status counts, typed operators and option counts, plus dated `activity_sources`. Counts are aggregate authorized reads, not a claim that all source data is complete.

Correction audit events record only the operation and changed facet names. They do not log values, raw answers or evidence text. Ordinary CSV imports and generic contact edits reject the protected namespace and preserve existing rating/review metadata.

## Reviewed source mapping manifest

Example manifest for an explicitly reviewed Luma question:

```json
{
  "mappings": [{
    "id": "career-stage-registration",
    "source_kind": "luma_answer",
    "source_key": "EXACT_LUMA_QUESTION_ID",
    "facet": "career_stage",
    "provenance": "self_reported",
    "self_report_confirmed": true,
    "reviewed": true,
    "aliases": {"junior": "early_career"}
  }]
}
```

The example question ID is a placeholder. A reviewer must verify the actual source and its wording before substituting it. Fuzzy question-label matching does not activate a mapping. Preview records use `mapping_id`, `source_record_id`, explicit timezone-aware `observed_at`, and `value` (string or string list). Unknown answers remain unmapped. A report of “Prefer not to say” remains withheld.

The refresh command reads already-ingested records. It defaults to a dry-run aggregate report and returns a `next_after_id` cursor. It supports exact Luma question IDs, existing CSV-origin fields or ordinary attributes, and explicit community-profile city/interests. It never calls Luma, Slack or an enrichment provider.

```sh
python manage.py crm_refresh_audience_profiles --mapping-file /absolute/path/mappings.json --limit 250
```

For a CSV mapping, supply `--observed-at` with the source observation time; the command does not substitute today's date. Source keys may be `city`, `job_title`, `company`, or an ordinary `attributes.<key>`. Protected/nested attributes are refused. Removed Luma registrations and missing previously mapped source records retract their contribution on refresh. Retraction preserves the original source observation time, is idempotent, and allows the same source record to be restored. Mapping IDs cannot contain the reserved `:` reference separator. Each changed contact and its audit event commit in the same transaction.

Writing a batch requires `--apply --actor-id <authorized MLAI admin ID>`. Review the dry-run and obtain the applicable operational authorization before a production backfill. Use the returned cursor for the next bounded page. The command creates no migration, sends no messages, and reports only counts and a cursor.

## Activity coverage

`--include-activity` reuses the existing batched activity collector and its exact identity rules. It projects only real dated event check-ins, public human Slack messages, explicit app logins and newsletter clicks. Registration is not check-in. Missing timestamps, private messages, background polling and name-based identity guesses do not become activity.

Each source stores `last_observed_at`, `covered_from`, `covered_through`, source `status` and `identity_status`. Source freshness is separate from complete historical interval coverage. The existing collector generally supplies partial source evidence, so the adapter does not invent complete coverage from a recent sync.

A known positive observation remains usable across an ingestion outage with coverage metadata. Negative activity and exclusions that depend on activity absence fail closed with `rule_not_available` / `activity_coverage_incomplete` unless every checked contact has verified identity and complete coverage for the requested interval. The interval ends at the earliest reliable source watermark, which must be no more than 48 hours old; the preview returns that `observation_as_of` timestamp and lag. Later verified positive observations are also retained, so a newer known event can never be mistaken for inactivity at the older watermark. This is explicit in the preview coverage metadata. The current guard conservatively checks the full authorized contact queryset. It may refuse a narrow negative audience when unrelated contacts are unmapped; it must not silently broaden that audience. SQL-null observations and missing JSON keys are handled explicitly so they cannot bypass the coverage check.

All stored timestamps use UTC with fixed microsecond precision for consistent typed JSON comparisons. Source adapters must supply explicit dates. External providers are not called by profile preview, correction or reviewed-import operations.

## Database-free validation

```sh
python scripts/test_without_database.py crm.tests.test_audience_profiles_unit crm.tests.test_audience_rules_unit
```

The runner forbids database connections and network access. These suites exercise normalization, missingness, evidence supersession/retraction, correction access, generic-field protection, compiler shapes and coverage failure behavior. Database-backed PostgreSQL membership/performance validation remains a separate check under the repository's specific migration/test approval policy.
