# Contacts store (`crm`)

Phase 1 of the contacts, segments and event-email plan
(`mlai-chat/docs/mlai/CONTACTS_EVENTS_EMAIL_PLAN_2026-10-23.md`). It stores
MLAI's Luma guests and imported opt-ins as contacts. There is no API, sending
or listmonk integration yet; those arrive in later phases.

## What is stored

| Table | Contents |
| --- | --- |
| `CrmEvent` | MLAI calendar events (managed Luma events only; external listings are skipped) and guest counts |
| `Contact` | One row per lowercase email: profile fields, tags, free-form `attributes`, email status, marketing consent, attendance roll-ups, optional link to an MLAI account |
| `EventRegistration` | One row per contact per event: Luma guest id, approval status, registration and check-in times, tickets, registration answers. Guests removed in Luma get `removed_at`, never a delete |
| `ContactConsentEvent` | Append-only evidence for consent and deliverability changes |
| `ContactImport` | One row per CSV import with counts and an error sample (row numbers and reasons only; no row data) |
| `CrmAuditEvent` | Append-only record of syncs, imports and, later, edits and sends |
| `Segment`, `SenderMailbox`, `EmailCampaign`, `EmailCampaignRecipient` | Created now so the schema needs one migration approval; unused until the sending phases |

Rules that hold everywhere:

- Luma is the source of truth for registrations and check-ins.
- Synced and imported data only fills blank contact fields. Manual edits and earlier values are never overwritten.
- Marketing consent is stored as `unknown`, `granted` or `withdrawn`. Consent inferred from recent attendance is computed at send time from `last_attended_at` and never stored.
- Nothing re-subscribes a contact who unsubscribed, bounced, complained or withdrew consent, or anyone with an active `AccountBan`.

## Luma guest sync

The `crm_luma_guest_sync` runner lives in `run_scheduled_discovery`. Each pass:

1. Upserts the calendar catalogue (`list_all_events`). It reads no guest data.
2. Picks up to `CRM_LUMA_SYNC_MAX_EVENTS_PER_RUN` events that are upcoming or ended within `CRM_LUMA_SYNC_RECENT_DAYS`, least recently synced first.
3. Replaces each event's registrations with Luma's current guest list, all approval statuses.

The runner is off unless `CRM_LUMA_SYNC_ENABLED=true`. It is cache-throttled to one pass per `CRM_LUMA_SYNC_INTERVAL_SECONDS`. Requests are spaced to `CRM_LUMA_REQUESTS_PER_MINUTE`, leaving room in the calendar key's 200/minute budget for Chat Home, Volunteer and Roo.

A failed runner stops the shared scheduler process, so the runner reports outcomes this way:

| Outcome | Status |
| --- | --- |
| Luma outage or rate limit | `deferred` |
| Some events failed | `partial`, listing `failed_events` |
| Luma rejected the key | `failed` |

A rejected key also breaks Chat Home.

If `CRM_LUMA_CONSENT_QUESTION_LABELS` lists a registration question label (pipe-separated), a "yes" answer to it grants marketing consent with source `luma:<event id>`.

### Backfill

```bash
python manage.py crm_backfill_luma_guests [--since YYYY-MM-DD] [--event-id ID ...] [--limit N] [--force]
```

- **Needs only `LUMA_API_KEY`**, not `CRM_LUMA_SYNC_ENABLED`.
- **Resumable:** events already synced are skipped unless you pass `--force`.
- **Rate limits:** on a 429 it pauses for 60 seconds, up to three times per event.
- **Failures:** failed events are listed and the command exits non-zero; re-run it to retry them.

## CSV import

```bash
python manage.py crm_import_contacts PATH --consent granted|none [--consent-source TEXT] \
  [--tag TAG ...] [--map HEADER=FIELD ...] [--actor-email EMAIL] [--dry-run]
```

- **Start with `--dry-run`.** It prints the column mapping and the counts, then rolls back. It still records a `ContactImport` row marked `dry_run`.
- **Column mapping:**
  - Common export headers are recognised (for example `Email Address`, `First Name`, `OPTIN_TIME`, `Tags`).
  - Use `--map` to override a mapping or ignore a column.
  - Any other column goes into `attributes`.
- **`--consent granted`** means everyone in the file opted in. A row's opt-in date and source columns become the consent evidence; otherwise the source is `csv:<file name>` and the date is the import date.
- **Rows:**
  - Emails are normalised and de-duplicated; later duplicates only fill blanks.
  - Invalid emails are counted, with their row numbers in the error sample.
  - No MX check is done yet.
- **Counts reported:** `created`, `updated`, `unchanged`, `invalid`, `duplicate`, `suppressed` and `consent_granted`.
- **Safe to re-run:** importing the same file twice changes nothing.

## Migration

`crm.0001_initial` is additive. It creates the ten tables above and touches no existing table. Like every migration it needs explicit approval and the `APPROVED_MIGRATION_PLAN_SHA256` cycle before deployment (see `README.md`). Tests:

- `crm.tests.test_luma_guests_unit` and `crm.tests.test_contact_import_unit` run without a database.
- `crm.tests.test_contact_store_database` runs in the CI PostgreSQL job.

## Ownership

| | |
| --- | --- |
| Owning package | `crm` |
| Entry points | the scheduler runner and the two management commands; no HTTP surface yet |
| Personal data | names, emails, phone numbers, employers and registration answers |
| Retention | retention undecided |
