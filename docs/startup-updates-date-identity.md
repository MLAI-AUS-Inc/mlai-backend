# Independent dated startup updates

Founders create individual publications. Calendar months organise the archive and retain their existing role as financial reporting periods. A publication date is not a new accounting period.

## API contract

- New founder saves send a UUID `creationKey` and an `updateDate` (`YYYY-MM-DD`). Reusing that creation key resolves the same startup-scoped draft. An exact first-save retry returns its existing revision; a changed or stale request returns 409.
- Subsequent saves and AI requests send `updateId` and `expectedRevision`. IDs are scoped to the selected company. A deliberate New update uses a new creation key, even on the same day.
- `month`/`year` remain on the save API for compatibility. A new draft's financial month comes from its initial date. Moving an existing publication's date preserves its financial month and frozen evidence.
- Publication responses expose `updateDate`, `datePrecision`, `firstPublishedAt`, `creationKey` and `narrativePeriod`. Published dates come from the approved revision while a replacement is edited. Month-only archives keep null dates; no import date is fabricated.
- The updates endpoint returns published records only. Drafts continue through the drafts endpoint. Audience filtering occurs before the UI computes month counts and titles.

## AI and evidence

The AI run captures one `update_id`, its base revision and a narrative interval. Default coverage starts at the preceding published update's explicit cutoff, falling back to the beginning of the selected month. The end is exclusive and capped at the update date/current time in the startup's reporting timezone. Founders can provide timezone-aware `narrativeStart` and `narrativeEnd` overrides.

Connector extraction uses the exact interval. Event dates have day precision and are selected from this run within those dates. Curation may include events across a calendar boundary while the draft and financial snapshot retain their original reporting month. The frozen snapshot includes the narrative interval, which the existing Valley evidence-snapshot drafting flow already consumes.

Independent runs cannot switch targets, overwrite a newer founder revision, or be reused by another draft. Cancellation restores the exact draft ID and preserves founder edits and approved publications. Legacy raw memo upserts cannot write independent runs; they must use the revision-aware worker endpoint.

Financial values remain source-owned. Repeated posts do not add repeated monthly amounts: histories select the latest frozen financial cutoff, and the frontend chart selects compatible provider/currency/period evidence. Completion rewards and reminders retain their existing monthly keys.

## Migration and rollout

`0023_independent_update_identity` adds nullable update dates, creation UUIDs and first-publication timestamps. It preserves existing IDs/revision foreign keys, copies historical publication timestamps and replaces the global organization/month uniqueness rule with:

- organization/creation-key uniqueness for independent drafts;
- organization/month uniqueness only for the legacy null-creation-key slot.

Scheduled imports and monthly jobs use the legacy slot explicitly. An older founder client trying to save by month where independent posts exist receives a conflict rather than selecting one arbitrarily.

Deploy the backend migration and compatibility API before the frontend PR. The migration is prepared for review, not applied to a live database by this change. Once independent same-month records exist, do not reverse to the old uniqueness constraint without an explicit data reconciliation plan. Rolling back the frontend alone preserves all records.

## Validation

Targeted tests cover independent saves, retries, company scoping, published visibility, date edits, exact worker targets, cancellation, revision conflicts, crossing month boundaries, DST, monthly rewards and reminders, and financial snapshot preservation. Local database tests use a disposable in-memory schema with unrelated migrations disabled. Migration graph/model-state agreement is checked separately. Production PostgreSQL migration execution and live OAuth/provider generation remain deployment checks.
