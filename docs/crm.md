# Contacts store (`crm`)

Phase 1 of the contacts, segments and event-email plan
(`mlai-chat/docs/mlai/CONTACTS_EVENTS_EMAIL_PLAN_2026-10-23.md`). It stores
MLAI's Luma guests and imported opt-ins as contacts, serves them to Chat
admins through the contacts API below, and sends blasts by email (through a
self-hosted listmonk), SMS and WhatsApp.

## What is stored

| Table | Contents |
| --- | --- |
| `CrmEvent` | MLAI calendar events (managed Luma events only; external listings are skipped) and guest counts |
| `Contact` | One row per lowercase email: profile fields, tags, free-form `attributes`, email status, marketing consent, phone (`phone_e164`, `sms_status`), WhatsApp opt-in, attendance roll-ups, optional link to an MLAI account |
| `EventRegistration` | One row per contact per event: Luma guest id, approval status, registration and check-in times, tickets, registration answers. Guests removed in Luma get `removed_at`, never a delete |
| `ContactConsentEvent` | Append-only evidence for consent and deliverability changes, per channel (email, SMS, WhatsApp) |
| `ContactImport` | One row per CSV import with counts and an error sample (row numbers and reasons only; no row data) |
| `CrmAuditEvent` | Append-only record of syncs, imports, edits, opt-outs and, later, sends |
| `Segment`, `SenderMailbox`, `MessageTemplate`, `Blast`, `BlastDelivery`, `InboundMessage` | Saved audiences, From addresses with daily caps, approved WhatsApp templates, multi-channel blasts (email, SMS, WhatsApp), one delivery row per person per channel (with opens and clicks for email), and replies to MLAI's number |

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
- **Who:** an MLAI admin or committee member: an active account with a linked, active `roo.PointsAdmin` row whose role is `admin` or `committee`, or a superuser (`roo.permissions.mlai_admin_role`). Portfolio leads, partners, Slack-ID-only bootstrap admins and Chat owners or admins don't count.
- **Session:** the account's own verified, unrevoked device installation (`community_chat.admin_access.is_mlai_admin_request`). A JWT session has no installation, so it is never an admin here.
- **Granting access:** add the person to the roster in `roo/management/commands/sync_committee_points_admins.py` and run it with `--apply`, or create a `PointsAdmin` linked to their account with role `admin` or `committee`.
- **Flag:** `COMMUNITY_CHAT_CRM_ENABLED` (default off).
- **`GET access/`** always answers 200 for a signed-in account, with `enabled`, `can_view` and `can_send`. Admins also get `luma_sync` and per-channel availability (`disabled` when a channel's flag is off, `not_configured` when a credential is missing). Others get `null` for both.
- **Every other route** answers 403 `not_authorised` to anyone who isn't an MLAI admin, whatever the flag says, and then 404 `crm_disabled` to admins while the flag is off. Every view subclasses `community_chat.admin_views.MlaiAdminView`; a route-audit test fails if one doesn't.
- **Services:** each write the views call (contacts, segments, uploads, blast drafts, queue, test send and cancel) also refuses a non-admin actor, so a caller that skips the views is still refused. Dispatch fails a live blast whose creator has lost admin (`last_error` `not_authorised`). The scheduler, commands and signature-verified Twilio webhooks have no actor and aren't gated.
- **`GET /api/v1/community-chat/admin/access/`** (MLAI Chat's Admin tab, not Django's `/admin/`) always answers 200 for a signed-in account: `{"is_admin": true, "sections": ["crm"]}` for an admin (`crm` only while the flag is on), `{"is_admin": false, "sections": []}` for anyone else. Throttle scope `community_chat_admin` (`COMMUNITY_CHAT_ADMIN_RATE`, default `120/minute`).

**Conventions**

- **Errors:** `{"error": code, "outcome": code}`, plus `fields` (error codes per field) for invalid input, and `pointer` (a JSON pointer) for an invalid segment definition.
- **Paging:** `?offset=&limit=` (limit 1 to 50, default 20). Lists return `{"results", "next"}`; the contact and guest lists also return `count` on the first page.
- **Throttle:** scope `community_chat_crm` (`COMMUNITY_CHAT_CRM_RATE`, default `240/minute`).
- **Responses:** `Cache-Control: no-store`.

**Routes**

| Route | Purpose |
| --- | --- |
| `GET overview/` | Contact totals, the next event and the last sent blast; `segments` (`total`, `updated_last_7_days`, the largest three as `top`), `email_30d` (`sent`: email rows that reached the provider in 30 days; `delivery_rate`: (sent - undelivered - bounced) / sent, or `null`) and `recent_activity` (the latest five audit rows as `action`, `created_at`, `actor_name` and a server-built `summary`; only the newest sync pass, no draft saves) |
| `GET events/?scope=upcoming\|past&q=` | Events with guest counts and per-channel reach |
| `GET events/{id}/` | One event with its blasts and quick segments (each with a saveable `definition`) |
| `GET events/{id}/guests/?status=going\|waitlist\|pending\|checked_in\|no_show\|all&q=` | Guest list. Removed guests never appear; `no_show` is empty until the event ends |
| `GET`, `POST contacts/` | Contact list with search and filters; add a contact by hand (409 `contact_exists` or `email_banned`) |
| `GET`, `PATCH contacts/{id}/` | Contact detail; edit profile fields, phone, tags and attributes |
| `POST contacts/{id}/stop-messaging/` | Opt out of email, SMS or WhatsApp. One-way |
| `GET tags/` | Tags in use, with counts |
| `POST imports/plan/` | Map CSV headers to fields (no row data is sent). Always 200, with `has_email: false` when no column maps to email, so the screen can still show the mapping |
| `GET`, `POST imports/`; `GET imports/{id}/` | Upload imports: start one, list them, read one |
| `POST imports/{id}/rows/` | Apply one chunk of up to 1,000 rows |
| `POST imports/{id}/finish/` | Close an upload |
| `POST imports/{id}/cancel/` | Stop a running upload (status `failed`, reason `stopped`). Rows already applied stay. Uploads with no chunk for an hour expire the same way (reason `expired`) |
| `GET`, `POST segments/`; `GET`, `PATCH`, `DELETE segments/{id}/` | Saved segments |
| `POST segments/preview/` | Count, a sample of 20 and exclusion counts for a definition |
| `GET segments/catalog/` | Rule types, fields, attribute keys, registration questions and tags for the builder |
| `GET mailboxes/` | From addresses with their daily cap, `sent_last_24h` and `remaining_today` |
| `GET whatsapp-templates/` | Approved, enabled WhatsApp templates and their slot mapping |
| `GET`, `POST blasts/`; `GET`, `PATCH blasts/{id}/` | Blasts (filters `status`, `event_id`, `channel`); create and edit drafts |
| `POST blasts/{id}/audience/` | People and reach per channel, exclusions, SMS segments and cost, caps |
| `POST blasts/{id}/test/` | Send the draft to the caller's own email and `User.phone` |
| `POST blasts/{id}/send/` | Queue it (`confirm` counts and `idempotency_key`) |
| `POST blasts/{id}/cancel/` | Cancel a draft, queued or sending blast |
| `GET blasts/{id}/deliveries/?channel=&status=` | One row per person per channel, addresses masked |
| `GET blasts/{id}/replies/` | Replies linked to the blast |

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
- **Rules:** every v1 rule type in the UI plan. `blast_engagement` matches people whose delivery of that blast was opened (a click counts as an open) or clicked, from the listmonk sync below.
- **Evaluation:** `evaluate(definition, purpose, event)` uses ORM `Q` and `Exists` expressions only and returns `(queryset, excluded)`. Suppression applies after the rules: banned, then unsubscribed (or marked as spam), then bounced. Then the purpose rule: `marketing` needs the consent rule, and `event_update` needs a registration for the event. Each excluded person is counted once, under the first reason that applies.
- **Answers:** registration-answer rules check the first 30 answers of each registration.
- **`last_count`:** the email audience with no purpose, recounted whenever the definition changes.

**Audit**

- **Writes:** every write leaves a `CrmAuditEvent`. Contact create and edit, segment create, update and delete, and blast draft create and edit, are `edit` rows that name the changed fields, never their values. Opt-outs are `opt_out`; uploads are `import`. Queueing, test sends and cancels are `send` rows with the counts per channel, never addresses.
- **Reads:** reads are not audited yet. `CrmAuditEvent.Action` has no read action, and adding one needs a migration.

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

## Blasts and listmonk

Steps B4 and B5 of `mlai-chat/docs/mlai/CONTACTS_UI_PLAN_2026-10-23.md`. Domain logic is in `crm/blasts.py` (audiences, queueing, dispatch, cancel, tests), `crm/blast_email.py` (the listmonk bridge) and `crm/listmonk_sync.py` (feedback); the client is `crm/listmonk.py`.

### Settings

| Setting | Purpose |
| --- | --- |
| `CRM_SENDING_ENABLED` | Email kill switch (default off). The dispatcher also runs while SMS or WhatsApp is on |
| `LISTMONK_BASE_URL` | listmonk on the `mlai-shared` network, e.g. `http://listmonk:9000`. Never hard-coded |
| `LISTMONK_API_USER`, `LISTMONK_API_TOKEN` | The listmonk API user, sent as HTTP Basic auth. Email is `not_configured` without all three |
| `LISTMONK_TEMPLATE_ID` | The MLAI campaign template's id; blank uses listmonk's default |
| `CRM_BLAST_DISPATCH_INTERVAL_SECONDS` | Dispatcher pass interval (default 60) |
| `CRM_LISTMONK_SYNC_INTERVAL_SECONDS` | Feedback sync interval (default 300) |

### listmonk setup (operator)

- **Version and network:** v6.2.x, pinned by digest. listmonk joins the `mlai-shared` network as `listmonk`; mlai-backend's API container (test sends) and scheduler container (both runners) must be on that network too.
- **API user** for mlai-backend with permissions to manage lists, subscribers and campaigns, send test campaigns, and run **SQL subscriber queries** (`subscribers:sql_query`). The feedback sync filters subscribers with SQL expressions; without that permission the sync fails, which stops the shared scheduler until it is fixed.
- **Template:** one MLAI-branded campaign template (`LISTMONK_TEMPLATE_ID`) with the sender identity and listmonk's unsubscribe link in the footer.
- **SMTP:** one SMTP server per sending domain, tagged with that domain (v6.2 per-domain routing). The campaign's From address picks the server, so never add two servers for one domain. Google Workspace domains use `smtp-relay.gmail.com`; bulk sends use Resend or SES on a dedicated subdomain.
- **Privacy settings:** individual subscriber tracking on (per-person opens and clicks), `List-Unsubscribe` header with one-click unsubscribe, blocklisting allowed.
- **Bounces:** bounce processing on, with the action "blocklist" for hard bounces.
- **Mailboxes:** add a `SenderMailbox` in Django admin for each From address (`daily_limit`, `reply_to`, `enabled`). Its domain must have an SMTP server in listmonk.

### Composing and sending

- **Drafts:** `POST blasts/` takes `purpose`, `event_id`, `quick_segment` (`going`, `checked_in`, `no_show`, `waitlist`) or `segment_id`, `channels`, `sms_only_without_whatsapp`, and the `email`, `sms` and `whatsapp` parts. SMS and WhatsApp need an `event_update` blast with an event (400 `channel_not_allowed`). Merge tags are checked on every save (400 `unsupported_merge_tag` with `field`; event tags without an event are 400 `merge_tag_needs_event`). A draft follows its saved segment until it is queued.
- **Audience:** email follows the segment's suppression and purpose rules (`segments.evaluate`). SMS and WhatsApp count people registered for the event: SMS needs an active mobile on an allowed prefix, WhatsApp needs opt-in and a number. With `sms_only_without_whatsapp`, anyone WhatsApp reaches gets no SMS (`covered_by_whatsapp`). Each excluded person is counted once, under the first reason. SMS segments and cost are counted with merge tags rendered for each recipient.
- **Caps:** email allows a mailbox's `daily_limit` minus its email deliveries in the last 24 hours (rows still waiting on a live blast count). SMS and WhatsApp use `CRM_SMS_DAILY_LIMIT` / `CRM_WHATSAPP_DAILY_LIMIT`.
- **`send/`** checks, in order: the blast is a draft (409 `blast_not_draft`); every channel is available (403 `channel_disabled` with `channel` and `reason`); the draft is complete (400, e.g. `mailbox_required`, `subject_required`, `sms_body_required`, `template_variable_missing`, `audience_required`); the counts equal `confirm` (409 `count_changed` with the fresh `audience/` body); and the caps (409 `daily_limit` with `limits`). It then snapshots one `BlastDelivery` per person per channel (lowercase email or E.164, with the consent basis: `event_registration`, `explicit` or `inferred_attendance`), sets `queued`, and writes a `send` audit row. The same `idempotency_key` returns the same blast; a key used by another blast is 409 `idempotency_key_reused`.
- **`test/`** sends only to the caller: email through listmonk's campaign test endpoint (the draft's list and campaign are created once and reused), SMS and WhatsApp through Twilio to the caller's `User.phone`. An unavailable channel answers 409 `channel_disabled`; no phone is 400 `test_phone_missing`.
- **`cancel/`** works on drafts, queued and sending blasts. Twilio stops between two messages. A running listmonk campaign is cancelled; its rows become `unknown`, since some may have gone. If listmonk can't be reached the blast is still cancelled and the sync retries the listmonk cancel (`last_error=listmonk_cancel_pending`).

### Runner `crm_blast_dispatch`

Cache-throttled to `CRM_BLAST_DISPATCH_INTERVAL_SECONDS`; off unless `CRM_SENDING_ENABLED`, `CRM_SMS_ENABLED` or `CRM_WHATSAPP_ENABLED` is on. Each pass claims up to 10 queued or sending blasts with `select_for_update(skip_locked=True)` and, for each:

1. **Email:**
   1. Finds or creates the list `crm-blast-<id>` and stores its id, with the blast row locked.
   2. Loads up to 500 recipients per pass (at most 45 seconds), in batches of 100: subscribers are looked up by `Contact.listmonk_subscriber_id` or email, created when missing, and added to the list. Consent is re-read first, so an opt-out since queueing wins; people blocklisted or unsubscribed in listmonk are dropped too (`unsubscribed` with an error code).
   3. Once everyone is loaded, finds or creates the campaign (`crm-blast-<id>`, Markdown, `From: Display <mailbox>`, `Reply-To` from the mailbox, tags `crm` and `blast:<id>`), stores its id, then sets it `running`.
   - List and campaign are always looked up by name before one is created, so a retry never makes a second.
   - Rows move `queued` → `submitting` (on the list) → `sent` when listmonk reports the campaign finished.
2. **SMS and WhatsApp:** `messaging.deliver_twilio_deliveries`, outside any transaction, sharing `CRM_TWILIO_SENDS_PER_TICK` across the pass.
3. **Status:** `sending` while anything is queued or in flight; then `sent`, `partially_sent` (one channel finished, another failed or was blocked by a disabled or unconfigured channel; rows stay `queued`) or `failed`. A disabled channel never sends an old blast later.

**Email content:** `{{ first_name }}` and `{{ display_name }}` become `{{ .Subscriber.FirstName }}` and `{{ .Subscriber.Name }}`; event tags are filled in server-side. Every other `{` is escaped (`&#123;` in Markdown, `{{ "{" }}` in the subject), so nothing in a body or an event name can run a template expression. Links written as `[text](https://…)` get `@TrackLink` for per-person click tracking.

**Outcomes:** a listmonk outage leaves rows queued and the runner `deferred`; listmonk refusing a blast (4xx) fails that blast's email rows; listmonk refusing the API credentials is the only `failed` (it stops the shared scheduler). One blast's unexpected error is logged and the pass goes on (`partial`).

### Runner `crm_listmonk_sync`

Runs whenever listmonk is configured, cache-throttled to `CRM_LISTMONK_SYNC_INTERVAL_SECONDS`. listmonk has no webhooks, so it polls:

- **Campaigns** of live blasts and of blasts queued or sent in the last 30 days: status and counts into `Blast.stats["email"]`; `finished` marks email rows `sent`.
- **Engagement**, for 30 days after a campaign starts: opens into `first_opened_at`, clicks into `first_clicked_at` and the tracked link into `clicked_urls`, hard and soft bounces into `bounced`, and unsubscribes from that blast's list into `unsubscribed`. Times are when the sync first saw the event, so they are accurate to the interval.
- **Opt-outs since the last pass:** blocklisted subscribers become `complained` (a complaint), `bounced` (a bounce) or `unsubscribed`; unsubscribes from any `crm-blast-*` list become `unsubscribed`. Each change writes an email `ContactConsentEvent` (source `listmonk:…`); unsubscribes and complaints also withdraw marketing consent; a bounce does not. A stronger status is never weakened.
- **Global blocklist:** unsubscribes are then blocklisted in listmonk, so unsubscribing means every list. Contacts opted out in MLAI Chat are blocklisted too.
- **Cursor:** the last pass time, kept in the cache and in a `sync` audit row (`target_type=listmonk`) whenever something changed. Each pass re-reads five minutes before it.

Outcomes: an outage is `deferred`; listmonk refusing the credentials or the SQL-query permission is `failed`; a blast whose campaign listmonk no longer has is skipped (`partial`).

Tests: `crm.tests.test_blasts_unit` and `crm.tests.test_listmonk_unit` run without a database; `crm.tests.test_blasts_database` runs in the CI PostgreSQL job against a fake listmonk and Twilio.

## Migration

`crm.0001_initial` is additive. It creates the twelve tables above and touches no existing table. Like every migration it needs explicit approval and the `APPROVED_MIGRATION_PLAN_SHA256` cycle before deployment (see `README.md`). Tests:

- `crm.tests.test_luma_guests_unit`, `crm.tests.test_contact_import_unit`, `crm.tests.test_phones_unit`, `crm.tests.test_api_unit`, `crm.tests.test_segments_unit`, `crm.tests.test_messaging_unit`, `crm.tests.test_blasts_unit` and `crm.tests.test_listmonk_unit` run without a database.
- `crm.tests.test_contact_store_database`, `crm.tests.test_api_database`, `crm.tests.test_segments_database`, `crm.tests.test_messaging_database` and `crm.tests.test_blasts_database` run in the CI PostgreSQL job.

Blasts and listmonk need no schema change: they use the tables in `crm.0001_initial`.

## Ownership

| | |
| --- | --- |
| Owning package | `crm` |
| Entry points | the scheduler runners (`crm_luma_guest_sync`, `crm_blast_dispatch`, `crm_listmonk_sync`), the management commands, the contacts API under `/api/v1/community-chat/crm/`, and the Twilio webhooks under `/api/v1/crm/webhooks/` |
| Personal data | names, emails, phone numbers, employers and registration answers |
| Retention | retention undecided |
