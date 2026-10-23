# Contacts store (`crm`)

Phase 1 of the contacts, segments and event-email plan
(`mlai-chat/docs/mlai/CONTACTS_EVENTS_EMAIL_PLAN_2026-10-23.md`). It stores
MLAI's Luma guests and imported opt-ins as contacts. There is no API, sending
or listmonk integration yet; those arrive in later phases.

## What is stored

| Table | Contents |
| --- | --- |
| `CrmEvent` | MLAI calendar events (managed Luma events only; external listings are skipped) and guest counts |
| `Contact` | One row per lowercase email: profile fields, tags, free-form `attributes`, email status, marketing consent, phone (`phone_e164`, `sms_status`), WhatsApp opt-in, attendance roll-ups, optional link to an MLAI account |
| `EventRegistration` | One row per contact per event: Luma guest id, approval status, registration and check-in times, tickets, registration answers. Guests removed in Luma get `removed_at`, never a delete |
| `ContactConsentEvent` | Append-only evidence for consent and deliverability changes, per channel (email, SMS, WhatsApp) |
| `ContactImport` | One row per CSV import with counts and an error sample (row numbers and reasons only; no row data) |
| `CrmAuditEvent` | Append-only record of syncs, imports and, later, edits and sends |
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

## Blasts by SMS and WhatsApp

Steps B6 and B7 of `mlai-chat/docs/mlai/CONTACTS_UI_PLAN_2026-10-23.md`. Event blasts can go out by SMS and WhatsApp through Twilio. Both channels are off until `CRM_SMS_ENABLED` / `CRM_WHATSAPP_ENABLED` are set.

### Twilio setup (operator)

1. In the Twilio console, confirm the WhatsApp-linked number can send SMS. If it can't, buy an Australian mobile number for SMS. Send from the numeric mobile number; use an alphanumeric "MLAI" sender only after registering it through Twilio.
2. Create a Messaging Service and add the number. Turn on **Advanced Opt-Out** (the default keywords plus `PAUSE`). Set the service's incoming-message URL to `https://api.mlai.au/api/v1/crm/webhooks/twilio/sms-inbound/`. Put its `MG…` SID in `TWILIO_MESSAGING_SERVICE_SID`.
3. Leave the WhatsApp sender's incoming-message URL on `…/api/content-factory/notification-webhooks/whatsapp/`. That handler keeps its research-automation behaviour and then passes every message to `crm.inbound`.
4. Submit WhatsApp **Utility** templates in the Content Template Builder, with fixed wording around the variables and "Reply STOP to opt out." in the copy. Starters are in the plan.
5. Run `python manage.py crm_sync_whatsapp_templates`. It upserts `MessageTemplate` rows by Content SID, refreshing name, body, category and approval, and never enables a template. New templates get every `{{n}}` slot as custom text.
6. In Django admin, map each slot of an approved template to `first_name`, `display_name`, `event_name`, `event_start_local`, `event_url` or `custom`, then tick `enabled`. Re-running the sync keeps those mappings.
7. Set `CRM_SMS_COST_PER_SEGMENT_AUD` and `CRM_WHATSAPP_COST_PER_MESSAGE_AUD` from current Twilio and Meta pricing, and adjust `CRM_SMS_DAILY_LIMIT` / `CRM_WHATSAPP_DAILY_LIMIT` (500 each by default) if needed.

Status callbacks need no console setting: every message carries `StatusCallback=<DEFAULT_BACKEND_URL>/api/v1/crm/webhooks/twilio/status/`.

### Sending (`crm/messaging.py`, `crm/twilio.py`)

- The blast dispatcher creates `queued` `BlastDelivery` rows (with `address` in E.164) and calls `deliver_twilio_deliveries(blast, limit=…)` outside a transaction, `limit` being at most `CRM_TWILIO_SENDS_PER_TICK`.
- Each row is claimed with `select_for_update(skip_locked=True)`, marked `submitting` and committed before the HTTP call.
- The contact's `sms_status` or WhatsApp opt-in is re-read just before each call, so a STOP that arrived after queueing still wins (the row becomes `unsubscribed`). SMS also needs a number matching `CRM_SMS_ALLOWED_PREFIXES` (default `+614`).
- **SMS body:** merge tags are rendered per person (`crm/merge_tags.py`), then `crm/sms.py` adds `MLAI: ` and `Reply STOP to opt out` and counts GSM-7 or UCS-2 segments into `segments`.
- **WhatsApp:** sends only an approved, enabled template. Its slots are filled from merge-tag sources or the blast's custom text.
- **Outcomes:**

  | Twilio answer | Delivery status |
  | --- | --- |
  | 2xx with a SID | `sent`, SID in `provider_id` |
  | 4xx | `failed`, Twilio's `code` in `error_code` |
  | timeout, connection error or 5xx | `unknown` |

  Nothing is retried. The Messages API has no idempotency key, so a retry could text someone twice. A person decides about `unknown` rows. A row left `submitting` for 15 minutes (a dispatcher that died mid-call) also becomes `unknown`.
- **Caps:** `remaining_today(channel)` counts rows queued in the last 24 hours that may have reached Twilio. Rows over the cap, or on a disabled or unconfigured channel, stay `queued`.

### Webhooks (`/api/v1/crm/webhooks/`)

Both endpoints check `X-Twilio-Signature` against the URL rebuilt from `DEFAULT_BACKEND_URL`. An unsigned request gets 403; anything handled or ignored gets 200.

| Path | Handles |
| --- | --- |
| `twilio/status/` | Updates the delivery by `MessageSid`. Order: queued < submitting < sent < delivered < read. `failed` and `undelivered` override `sent`, never `delivered` or `read`, and are final. Replays and unknown SIDs change nothing. |
| `twilio/sms-inbound/` | SMS replies and keywords, answered with empty TwiML |

### Replies and opt-outs (`crm/inbound.py`)

- **Opt-out keywords:** the whole message, trimmed and case-insensitive: `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT` and `PAUSE`. Twilio's `OptOutType=STOP` counts too. Every contact with that `phone_e164` is opted out of that channel only (`sms_status=opted_out` or `whatsapp_opt_in=withdrawn`), with a `ContactConsentEvent` per contact.
- **Opting back in:** `START` / `UNSTOP` turn back on only the channel they arrived on. SMS returns to `active` only for a textable number; WhatsApp becomes `granted` with source `whatsapp:inbound`. `JOIN` on WhatsApp also grants opt-in.
- **Replies:** any other text becomes an `InboundMessage`, idempotent on the Twilio SID. It is linked to the contact and to the latest blast that reached them on that channel within 7 days. Messages from numbers no contact has are not kept.
- WhatsApp's research-automation STOP handling is unchanged. A crm failure is logged and never changes that webhook's result.

Tests: `crm.tests.test_messaging_unit` runs without a database; `crm.tests.test_messaging_database` runs in the CI PostgreSQL job.

## Migration

`crm.0001_initial` is additive. It creates the twelve tables above and touches no existing table. Like every migration it needs explicit approval and the `APPROVED_MIGRATION_PLAN_SHA256` cycle before deployment (see `README.md`). Tests:

- `crm.tests.test_luma_guests_unit` and `crm.tests.test_contact_import_unit` run without a database.
- `crm.tests.test_contact_store_database` runs in the CI PostgreSQL job.

## Ownership

| | |
| --- | --- |
| Owning package | `crm` |
| Entry points | the scheduler runner and the two management commands; no HTTP surface yet |
| Personal data | names, emails, phone numbers, employers and registration answers |
| Retention | retention undecided |
