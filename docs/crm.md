# Contacts store (`crm`)

Phase 1 of the contacts, segments and event-email plan
(`mlai-chat/docs/mlai/CONTACTS_EVENTS_EMAIL_PLAN_2026-10-23.md`). It stores
MLAI's Luma guests and imported opt-ins as contacts, and serves them to Chat
admins through the contacts API below. Sending and the listmonk integration
arrive in later phases.

## What is stored

| Table | Contents |
| --- | --- |
| `CrmEvent` | MLAI calendar events (managed Luma events only; external listings are skipped) and guest counts |
| `Contact` | One row per lowercase email: profile fields, tags, free-form `attributes`, email status, marketing consent, phone (`phone_e164`, `sms_status`), WhatsApp opt-in, attendance roll-ups, optional link to an MLAI account |
| `EventRegistration` | One row per contact per event: Luma guest id, approval status, registration and check-in times, tickets, registration answers. Guests removed in Luma get `removed_at`, never a delete |
| `ContactConsentEvent` | Append-only evidence for consent and deliverability changes, per channel (email, SMS, WhatsApp) |
| `ContactImport` | One row per CSV import with counts and an error sample (row numbers and reasons only; no row data) |
| `CrmAuditEvent` | Append-only record of syncs, imports, edits, opt-outs and, later, sends |
| `Segment`, `SenderMailbox`, `MessageTemplate`, `Blast`, `BlastDelivery`, `InboundMessage` | Saved audiences, From addresses, approved WhatsApp templates, multi-channel blasts (email, SMS, WhatsApp), one delivery row per person per channel, and replies to MLAI's number. Created now so the schema needs one migration approval; unused until the blast phases |

Rules that hold everywhere:

- Luma is the source of truth for registrations and check-ins.
- Synced and imported data only fills blank contact fields. Manual edits and earlier values are never overwritten.
- Marketing consent is stored as `unknown`, `granted` or `withdrawn`. Consent inferred from recent attendance is computed at send time from `last_attended_at` and never stored.
- Nothing re-subscribes a contact who unsubscribed, bounced, complained or withdrew consent, or anyone with an active `AccountBan`.
- Phone numbers are normalised to E.164 (`crm/phones.py`; Australian local formats are accepted). `sms_status` is `active` for a textable number, `invalid` for an Australian landline, and an SMS opt-out always sticks.
- A phone number is never WhatsApp opt-in. Opt-in comes only from a Luma question named in `CRM_LUMA_WHATSAPP_QUESTION_LABELS`, a CSV `whatsapp` column, or (later) the person messaging MLAI's number.

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

## API

Contacts API for MLAI Chat (UI plan:
`mlai-chat/docs/mlai/CONTACTS_UI_PLAN_2026-10-23.md`, steps B1 to B3). Routes
live under `/api/v1/community-chat/crm/`, the prefix the desktop CORS
middleware allows. Views are in `community_chat/crm/`; queries, segments and
access rules are in `crm/queries.py`, `crm/segments.py` and `crm/access.py`.

**Access**

- **Authentication:** the Volunteer API's classes (`CommunityChatAccountAuthentication`, `CustomJWTAuthentication`).
- **Who:** a Chat owner or admin, resolved by `account_chat_role` on the session's own verified device installation. A JWT session has no installation, so it is never an admin here.
- **Flag:** `COMMUNITY_CHAT_CRM_ENABLED` (default off).
- **`GET access/`** always answers 200 for a signed-in account, with `enabled`, `can_view` and `can_send`. Admins also get `luma_sync` and per-channel availability (`disabled` when a channel's flag is off, `not_configured` when a credential is missing). Others get `null` for both.
- **Every other route** answers 404 `crm_disabled` while the flag is off, and 403 `not_authorised` to everyone but owners and admins.

**Conventions**

- **Errors:** `{"error": code, "outcome": code}`, plus `fields` (error codes per field) for invalid input, and `pointer` (a JSON pointer) for an invalid segment definition.
- **Paging:** `?offset=&limit=` (limit 1 to 50, default 20). Lists return `{"results", "next"}`; the contact and guest lists also return `count` on the first page.
- **Throttle:** scope `community_chat_crm` (`COMMUNITY_CHAT_CRM_RATE`, default `240/minute`).
- **Responses:** `Cache-Control: no-store`.

**Routes**

| Route | Purpose |
| --- | --- |
| `GET overview/` | Contact totals, the next event and the last sent blast |
| `GET events/?scope=upcoming\|past&q=` | Events with guest counts and per-channel reach |
| `GET events/{id}/` | One event with its blasts and quick segments (each with a saveable `definition`) |
| `GET events/{id}/guests/?status=going\|waitlist\|pending\|checked_in\|no_show\|all&q=` | Guest list. Removed guests never appear; `no_show` is empty until the event ends |
| `GET`, `POST contacts/` | Contact list with search and filters; add a contact by hand (409 `contact_exists` or `email_banned`) |
| `GET`, `PATCH contacts/{id}/` | Contact detail; edit profile fields, phone, tags and attributes |
| `POST contacts/{id}/stop-messaging/` | Opt out of email, SMS or WhatsApp. One-way |
| `GET tags/` | Tags in use, with counts |
| `POST imports/plan/` | Map CSV headers to fields (no row data is sent) |
| `GET`, `POST imports/`; `GET imports/{id}/` | Upload imports: start one, list them, read one |
| `POST imports/{id}/rows/` | Apply one chunk of up to 1,000 rows |
| `POST imports/{id}/finish/` | Close an upload |
| `GET`, `POST segments/`; `GET`, `PATCH`, `DELETE segments/{id}/` | Saved segments |
| `POST segments/preview/` | Count, a sample of 20 and exclusion counts for a definition |
| `GET segments/catalog/` | Rule types, fields, attribute keys, registration questions and tags for the builder |

**Contacts**

- **Search (`q`):** every word must match an email, a name or the company. A phone-like query (at least four digits) also matches `phone_e164`, so `0412 345` finds `+61412345…`.
- **Marketing eligible:** `email_status` is `active`, there is no active `AccountBan`, consent is not withdrawn, and consent is either granted or inferred from attendance in the last 730 days.
- **Phone edits:** numbers go through `crm/phones.py`. A new number resets `phone_e164` and `sms_status`, but an SMS opt-out always sticks. A number that can't be read is refused with `invalid_phone`.
- **`stop-messaging`:**
  - email sets `email_status=unsubscribed` (a bounce or spam complaint is kept) and withdraws marketing consent;
  - SMS sets `sms_status=opted_out`;
  - WhatsApp sets `whatsapp_opt_in=withdrawn`.
  Each channel gets a `ContactConsentEvent`, and each request one `opt_out` audit row.

**Events and reach**

- **Counts:** `going` is `approved`, `pending` is `pending_approval`, and `no_show` is going guests who did not check in, counted once the event has ended.
- **Reach** counts registrations that are not removed and not declined:
  - email needs `active` and no ban;
  - SMS needs `sms_status=active` and a number starting with one of `CRM_SMS_ALLOWED_PREFIXES` (default `+614`);
  - WhatsApp needs opt-in and a number on file.

**Upload imports** (`crm/contact_import.py`)

- **Start:** `start_upload_import` stores the edited column plan, which must map exactly one column to `email`, and records `origin=upload`.
- **Chunks:** each chunk of `{header: value}` rows goes through the same `_parse_row` and `_apply_chunk` path as `crm_import_contacts`.
  - `start_row` is the zero-based index of the chunk's first row and must equal `rows_received`. A re-sent chunk is answered without being applied again.
  - Counts are merged with the import row locked (`select_for_update`).
  - A dry run applies each chunk inside a transaction that is rolled back.
- **Refusals:**
  - more than 1,000 rows: 400 `too_many_rows`;
  - more rows than `row_count`: 400 `row_count_exceeded`;
  - an import that is not a running upload: 409 `import_not_running`.
- **Audit:** starting and finishing each write an `import` audit row with the counts.
- **Duplicates across chunks:** duplicate emails are merged within a chunk only. Across chunks, a repeat counts as `updated` or `unchanged` (in a dry run, `created` twice) instead of `duplicate`. Without cross-chunk repeats, the counts match the command on the same file.

**Segments** (`crm/segments.py`)

- **Validation:** `validate_definition` is pure and returns the canonical definition, or 400 `invalid_definition` with a JSON pointer and a reason.
- **Rules:** every v1 rule type in the UI plan. `blast_engagement` validates and can be saved (with `last_count` null), but evaluating it answers 400 `rule_not_available` until engagement data exists (step B5).
- **Evaluation:** `evaluate(definition, purpose, event)` uses ORM `Q` and `Exists` expressions only and returns `(queryset, excluded)`. Suppression applies after the rules: banned, then unsubscribed (or marked as spam), then bounced. Then the purpose rule: `marketing` needs the consent rule, and `event_update` needs a registration for the event. Each excluded person is counted once, under the first reason that applies.
- **Answers:** registration-answer rules check the first 30 answers of each registration.
- **`last_count`:** the email audience with no purpose, recounted whenever the definition changes.

**Audit**

- **Writes:** every write leaves a `CrmAuditEvent`. Contact create and edit, and segment create, update and delete, are `edit` rows that name the changed fields, never their values. Opt-outs are `opt_out`; uploads are `import`.
- **Reads:** reads are not audited yet. `CrmAuditEvent.Action` has no read action, and adding one needs a migration.

## Migration

`crm.0001_initial` is additive. It creates the twelve tables above and touches no existing table. Like every migration it needs explicit approval and the `APPROVED_MIGRATION_PLAN_SHA256` cycle before deployment (see `README.md`). Tests:

- `crm.tests.test_luma_guests_unit`, `crm.tests.test_contact_import_unit`, `crm.tests.test_phones_unit`, `crm.tests.test_api_unit` and `crm.tests.test_segments_unit` run without a database.
- `crm.tests.test_contact_store_database`, `crm.tests.test_api_database` and `crm.tests.test_segments_database` run in the CI PostgreSQL job.

## Ownership

| | |
| --- | --- |
| Owning package | `crm` |
| Entry points | the scheduler runner, the two management commands, and the contacts API under `/api/v1/community-chat/crm/` |
| Personal data | names, emails, phone numbers, employers and registration answers |
| Retention | retention undecided |
