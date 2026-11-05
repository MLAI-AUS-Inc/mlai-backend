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

1. Upserts all approved calendar listings (`list_crm_events`): managed Luma events, Luma events managed elsewhere, and external listings. It exhausts pagination before writing and reads no guest data. An event appears once, keyed by its Luma event/listing ID.
2. Picks up to `CRM_LUMA_SYNC_MAX_EVENTS_PER_RUN` **managed Luma events** that are upcoming or ended within `CRM_LUMA_SYNC_RECENT_DAYS`, least recently synced first. View-only and external listings never enter guest sync, including resumed IDs and backfill commands.
3. Replaces each event's registrations with Luma's current guest list, all approval statuses. Each event commits on its own.

The runner is off unless `CRM_LUMA_SYNC_ENABLED=true`. It is cache-throttled to one pass per `CRM_LUMA_SYNC_INTERVAL_SECONDS`. Requests are spaced to `CRM_LUMA_REQUESTS_PER_MINUTE`, leaving room in the calendar key's 200/minute budget for Chat Home, Volunteer and Roo.

CRM event summaries include `guest_data_status`: `available` after a managed
event's first guest sync, `pending` before it, or `unavailable` for a view-only
or external listing. Counts and reach are stored registration statistics, not
the host's live guest totals; clients must not present them as current when
this status is not `available`. Previously synced registrations and contact
preferences are retained if an event's calendar access changes.

A calendar API key cannot discover every event hosted by a separate Luma user.
Hosted events outside this calendar require an authenticated source for that
host account; calendar coverage alone must not be reported as account coverage.

### Pass budget

The runner shares the scheduler tick with every other runner, `crm_blast_dispatch` included. The tick must finish for the scheduler to stay healthy: its health file goes stale after `SCHEDULED_DISCOVERY_HEALTH_MAX_AGE_SECONDS` (300) without a successful tick, and a deploy waits about two minutes for one. At 100 requests a minute and 100 guests a page, a full pass takes minutes. The first production pass (26 Oct 2026: 8 events, 13,462 registrations) held up the tick and failed the deploy's scheduler check.

So a pass starts no new event once `PASS_SECONDS` (60, in `crm/luma_sync.py`) have passed:

- **In flight:** the current event still finishes, so one very large event can overrun the budget.
- **Hand-over:** the events not reached are returned as `events_left` and kept in the cache (`crm:luma-guest-sync:resume`), and the throttle is released.
- **Next tick:** about a minute later, it reads only those events, in the same order, with no catalogue refresh and a fresh 60-second budget. It can hand over again, and each of these ticks reads at least one event.
- **Finish:** the pass that reads the last of them keeps the throttle, so the next full pass starts an interval after it.
- **Errors:** an outage or a rejected key drops the hand-over, and a rate limit ends the pass without one. The next full pass still takes the least recently synced events first.

A failed runner stops the shared scheduler process, so the runner reports outcomes this way:

| Outcome | Status |
| --- | --- |
| Luma outage or rate limit | `deferred` |
| Some events failed | `partial`, listing `failed_events` |
| Ran out of time | `completed` (or `partial`), listing `events_left` for the next tick |
| Luma rejected the key, at any point in a pass | `failed` |

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

## Resend feedback and a finite email rollout

`POST /api/v1/crm/webhooks/resend/` verifies the raw body against
`CRM_RESEND_WEBHOOK_SECRET` (Svix HMAC, five-minute timestamp tolerance).
Production's provider URL is `https://lists.mlai.au/webhooks/resend`, whose
Nginx exact route forwards to this handler. Unlike the general API's existing
Cloudflare Flexible ingress, the lists host uses Full TLS to the origin.
Listmonk admin/API paths remain private.

Subscribe to `email.sent`, `email.delivered`, `email.bounced`,
`email.complained`, `email.failed`, `email.delivery_delayed` and
`email.suppressed`. All configured stream domains and protected domains are
observed. `CRM_RESEND_MONITORED_SENDERS` adds legacy pilot addresses; it no
longer filters out other stream or system senders. One SMTP recipient per
message is required. The signing secret is never a sending API key.

`EmailEvent` stores bounded feedback, sender/recipient, message identity,
masked diagnostics and delivery/blast/stream correlation. Its unique hashed
Svix receipt deduplicates concurrent retries. Historical `resend_event`
audit receipts remain replay barriers; replaying one never reapplies a
consent or streak change. The callback uses no sender row locks. Late success
never restores a hard bounce or complaint. Permanent bounces and complaints
suppress the contact and blocklist its listmonk subscriber; blocklisting is
retried on duplicate negative feedback after an outage. Storage or suppression
failures return 503 for provider retry.

Temporary bounces are `undelivered` with `soft_bounce`, counted once per
contact per blast. Three successive affected blasts suppress the address; a
later delivered event resets the streak without restoring consent or undoing
permanent suppression. Out-of-order older feedback cannot reset a newer
bounce streak. Listmonk engagement sync treats only hard bounces as permanent;
Resend supplies the temporary-bounce classification.

A feedback callback never disables senders or pauses campaigns. The
`crm_deliverability_guard` scheduler runner evaluates rate/minimum-sample
rules before blast dispatch. One hard bounce among 1,000 attempted messages
does not pause a blast; a hard-bounce rate of 5% or more at sample 100+ does.
Complaint rates require at least two complaints. Three provider policy
rejections stop the affected campaign stream. Simulator recipients are
excluded. Repeated detections refresh one open incident, automatic decisions
are audited, and a failed provider pause is retried with admission already
paused. A recent completed guardian pass and verified sending-domain checks
are required for new stream campaign starts. The runner never raises into
the shared scheduler. Its flag defaults off; production refuses dry-run
admission. Transactional streams are excluded from campaign rules.

The finite legacy warm-up retains its own bounded admission and stop policy;
it is not the bulk stream's rate policy. Do not extend or retire that pilot
before its final check on 17 November 2026.

`crm_warmup` uses ordinary draft `Blast` rows with immutable planned metadata
in `stats.warmup`. There is no new database table or migration. The single
active run is selected by `CRM_WARMUP_RUN_ID`; `CRM_WARMUP_ENABLED` defaults off.

```sh
python manage.py crm_warmup import --actor-email sam@mlai.au --dry-run
python manage.py crm_warmup plan --run-id mlai-20261027 --start-date 2026-10-27 --actor-email sam@mlai.au --dry-run
# Inspect the counts, then repeat without --dry-run to save drafts.
python manage.py crm_warmup activate --run-id mlai-20261027
python manage.py crm_warmup status --run-id mlai-20261027
```

Import requires an approved active member, verified email, affirmative current
profile choice and latest affirmative `CommunityMemberConsent` evidence. It
preserves the evidence's actual timestamp and policy version, fills only blank
profile fields and never revives CRM withdrawals, unsubscribes, bounces, complaints
or bans. The source is checked again at queueing and campaign start. During
the run, withdrawals in the community profile are copied back to CRM.

The saved schedule assigns each person to one sender and one weekday/time,
10am–4pm Melbourne time. Week one includes only members active within 90 days;
weeks two and three include the full saved eligible roster. There is a rolling
seven-day frequency cap across CRM email, one outstanding admission per sender
per hour, the existing mailbox daily caps, and a hard maximum of 56 people.
New opt-ins do not enlarge an already planned roster. Missed windows older than
six hours are cancelled, not caught up in a burst. Warm-up emails have no tracked
link redirects and use the normal unsubscribe template and headers.

Activation requires signed delivered/bounced/complained simulator receipts for
both senders within 24 hours. The server scheduler runs admission before the
normal blast dispatcher. It pauses the run if any delivery fails, either sender
is disabled, or a queued email lacks a terminal delivery result after one hour.
It finishes after 21 days; it never raises caps or starts a larger campaign.
Completion proves only the actual low-volume delivery history, not guaranteed
inbox placement or readiness for a large mailing.

The planned 27 October–16 November 2026 schedule has 41 eligible contacts,
18 recently active: 18/41/41 messages by week, 100 total across two senders.
The final check is on 17 November. Copy is in `crm/warmup.py`: community links,
finding events, then ways to participate. Replies go to `hi@mlai.au`.

Database-free checks: `scripts/test_without_database.py crm.tests.test_resend_warmup_unit`.
Database regression checks: `manage.py test crm.tests.test_resend_warmup_database`.
The user approved the unchanged 374-migration disposable test inventory on
26 October 2026, SHA-256
`6571effcd2fba39449b5984517314d0774d15f17e450fe1a8259d518832e1530`.
This approval does not cover production migrations.

## CSV import

### Reviewed engagement snapshots

`crm_import_engagement` imports the reviewed merged contact CSV into the existing
contact store. It uses existing JSON attributes; no migration is required.

```bash
python manage.py crm_import_engagement /private/path/merged.csv \
  --expected-sha256 <reviewed-file-sha256> --actor-email <MLAI-admin-email> \
  --permission-evidence /private/path/luma-marketing-evidence.json
# Review the rollback-only counts, then repeat with --apply to persist.
```

The complete file is validated before writes. Duplicate addresses, invalid
ratings and a checksum mismatch abort the operation. The default preview rolls
back all writes, including its import/audit records. Application is atomic and
reimporting the same snapshot leaves contact data unchanged. An older snapshot
cannot replace a newer rating. Banned addresses are skipped.

The import stores typed `engagement_rating` metadata (stars, score, rule version,
as-of date, reason and source coverage) and namespaced source evidence. It fills
only blank display names. It does not modify existing consent, email/SMS/WhatsApp
status, opt-out timestamps, provider IDs or consent-event history. It does not
create blasts/deliveries or call a mail provider. Positive or partner-scoped form
answers remain evidence for review, never an automatic marketing consent grant.

New contacts, existing contacts with unknown permission, and contacts with a
declined MLAI/event-update preference receive a monotonic
`marketing_review_hold`. Existing holds are never cleared by an import. This is
a review flag, not a formal unsubscribe or consent withdrawal. Marketing
audience selection, send-time loading/campaign start and warm-up admission honor
the hold. Source-consent synchronization still processes genuine withdrawals
without turning the review flag itself into an unsubscribe.

Admin contact list/detail responses expose `engagement_rating` (or null),
`engagement_stars`, `engagement_score` and `marketing_review_hold`. Filters accept
`stars`, `min_stars`, `max_stars` (1–5); `sort=engagement` ranks stars then score,
with unrated contacts last. Segments support an `engagement_stars` rule. Ordinary
attribute edits cannot replace reserved ratings or remove a review hold.

The 2 November 2026 import is a provisional snapshot. Daily refresh (below)
uses dated source evidence without turning unknown coverage into inactivity.
One star can reflect limited observed evidence.

### Daily engagement refresh

`CRM_ENGAGEMENT_REFRESH_ENABLED=true` enables the activity-only scheduler runner.
`CRM_ENGAGEMENT_REFRESH_HOUR=6` starts each day's sweep after 06:00 in
Australia/Melbourne, including daylight saving. Thirty-five-second passes
resume on subsequent ticks, with a shared Redis lock, per-contact day/version
receipts and a durable completed-day audit record. Repeated ticks are cheap.
Transient expected-source failures preserve the previous rating and retry after
15 minutes. A permanently unavailable source is explicitly partial coverage.

The job reads:

- Luma's existing successfully ingested registrations and actual check-ins.
  Dated imported history outside the calendar key remains available; only a
  complete newer sync can replace that event's historical evidence.
- Public Slack human-message metadata from the bridge, dated by source Slack
  timestamp. Bots, reactions, system notices, private/DM and test activity are
  excluded. Exact active account email mappings are required.
- Explicit `User.last_login` recency. This contributes at most five points;
  background refresh/device timestamps are not usage frequency.
- Dated CRM email-click feedback and the scheduled Substack browser export.
  `CRM_SUBSTACK_ACTIVITY_ENABLED=true` expects a daily authenticated export,
  delivered by the local Codex task at 05:30 before the 06:00 rating sweep.
  This uses the supported subscriber export, not an undocumented API or copied
  session credentials. The Mac, Codex app and signed-in official Chrome
  connection must be available. A stale expected export preserves prior ratings
  and is visible in source coverage. Historical evidence ages normally.
  Undated aggregate counters cannot become invented activity dates, and email
  opens always earn zero points. See [the export runbook](crm-substack-refresh.md).

Only rating/refresh metadata in `Contact.attributes` is changed. Consent,
unsubscribe/delivery states, review holds, consent history, blasts, provider
audiences and sending configuration are not modified. Malformed legacy
attributes are retained under a protected key and remain effectively held.

`crm_refresh_engagement --actor-email <admin>` is rollback-only by default;
`--apply` executes a bounded pass. It never invokes source synchronization or a
sender. The initial dated-history seed command is also rollback-first and
requires exact input checksums. Namespace keys are protected from ordinary
attribute editing.

`crm_import_newsletter_activity PATH --expected-sha256 SHA --exported-at ISO
--actor-email ADMIN` previews an export; `--apply` persists only dated click
evidence for existing contacts. It never imports subscribers or preferences,
infers opt-outs from missing rows, or changes a review hold. An older/equal
export is idempotently skipped. Freshness is published only after commit and is
durably recorded even when the export contains no new clicks.

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
| `GET`, `POST mailboxes/`; `GET`, `PATCH mailboxes/{id}/` | From addresses (senders); writes require a deliverability owner. Responses include `domain`, daily cap, `sent_last_24h` and `remaining_today`; add one (`email`, `display_name`, `reply_to`, `daily_limit`, `enabled`) or edit one (the address is fixed). The address must be on a `CRM_SENDING_DOMAINS` domain (400 `sending_domain_not_configured` with `domain`), which also sets `route`; a taken address is 409 `mailbox_exists`; switching a sender on needs its domain `verified` (400 `sending_domain_unverified`). Switching one off holds its blasts that haven't started; listmonk finishes ones already running |
| `GET sending-domains/?refresh=1` | Each `CRM_SENDING_DOMAINS` domain with `route`, `status` (`verified`, `failing` or `unknown` when DNS didn't answer), `checked_at`, `mailbox_count` and its `records` (`key`, `label`, `type`, `host`, `expected`, `required`, `status` `pass`/`fail`/`error`, `detail`, `found`). Domains only a stored sender still names follow with `configured: false` and status `not_configured`. Answers are cached for five minutes; `refresh=1` checks again |
| `GET whatsapp-templates/` | Approved, enabled WhatsApp templates and their slot mapping |
| `GET`, `POST blasts/`; `GET`, `PATCH blasts/{id}/` | Blasts (filters `status`, `event_id`, `channel`); create and edit drafts |
| `POST blasts/{id}/audience/` | People and reach per channel, exclusions, SMS segments and cost, caps |
| `POST blasts/{id}/plan/` | Read-only email hygiene, activity tiers, provider mix and expected sending-day schedule |
| `POST blasts/{id}/test/` | Send the draft to the caller's own email and `User.phone` |
| `POST blasts/{id}/send/` | Queue it (`confirm` counts and `idempotency_key`) |
| `POST blasts/{id}/cancel/` | Cancel a draft, queued or sending blast |
| `POST blasts/{id}/pause/`, `POST blasts/{id}/resume/` | Staff pause and resume their own pause; a system pause requires owner incident acknowledgement |
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

### Audience builder and definition version 2

The Admin audience builder uses the existing CRM routes and `Segment.definition`
JSON column. It requires the existing MLAI admin/verified-installation gate.
`CRM_SEGMENT_BUILDER_V2_ENABLED` defaults to true; setting it false disables new
v2 segment creation and edits while preserving reads, previews, evaluation and
queued campaigns. It does not enable sending or change any provider cap.

The unversioned v1 `{match, rules, exclude}` representation remains accepted.
New definitions use `{version: 2, include: node, exclude: node | null}`. Nodes are:

- `{kind: "all_contacts"}`: explicitly match all contacts.
- `{kind: "rule", rule: ...}`: an existing or typed audience leaf.
- `{kind: "group", match: "all" | "any", children: [...]}`: nonempty nested AND/OR.
- `{kind: "saved_segment_snapshot", source_segment_id, source_definition_hash,
  source_name, captured_at, definition}`: a complete embedded v2 definition.

The top-level exclusion is subtracted after evaluating the inclusion tree.
Exclusions inside a snapshot apply only within that snapshot, including when
it is one branch of an OR group. A copied segment pins its rules, while matching
contacts continue to change as their facts change. It does not follow a live
segment reference. New copies are checked against their source on save;
unchanged copies already persisted in the edited segment remain valid after
source edits/deletion. Refreshing a copy requires explicitly replacing it with
the current source definition. Copying preserves the entire group and exclusion
structure rather than flattening rules.

The shared validation budget is 50 leaves, 100 structural nodes, six nested
levels, ten snapshots and 64 KiB of serialized JSON, including every embedded
copy. Lists retain the existing 50-item limit. Empty v2 groups are invalid;
legacy empty-rule definitions retain their prior match-everyone meaning.
Errors carry a JSON pointer. All predicates compile to allowlisted Django ORM
expressions; no SQL or arbitrary lookup paths are accepted.

Typed leaves are `location_affinity`, `audience_facet` and `activity`.
`location_affinity` represents connections to cities, separately from the
`reported_city` facet. The protected `audience_profile_v1` attributes namespace
holds normalized facts and provenance; generic attribute rules cannot query
that object as text. Career and stage are distinct facets, gender/age require
self-report evidence, and absent/withheld/conflicted/unmapped facts are distinct
states. Ordinary negative value matching requires known facts. Negative or
excluded activity fails closed when complete source/identity coverage for the
requested interval is unavailable. Absence-dependent windows use a common,
explicit source observation cutoff no more than 48 hours old; the preview's
`activity_coverage` reports that cutoff and lag. Newer positive observations
still prevent a contact being treated as inactive. Positive dated observations remain usable
without interpreting a source outage as inactivity. `engagement_stars` also
accepts `is_set` and `is_not_set`, without a value; unrated is never one star.

The catalogue adds `builder_enabled`, `supported_definition_versions`,
`capabilities`, structural `limits`, typed `facets` and `activity_sources`.
Facet availability and counts come from actual protected profiles. An unmapped
field is not advertised as populated merely because an imported column has a
similar name. The profile correction and source-mapping paths preserve ordinary
attributes and cannot change consent or deliverability fields.

Segment list requests support `q` (name/description search) and `ordering`
(`name`, `-updated_at`, `-last_count`) with bounded existing pagination.
Every segment DTO includes `definition_version`, `definition_hash` and `etag`;
single-record reads and saves also return the HTTP `ETag` header. New clients
send that exact token in `If-Match` for edits/deletes. Comparison and mutation
occur under a row lock; a stale edit returns 409 `segment_changed` with the latest
record. V2 edits without a token return 428 `segment_precondition_required`.
Legacy clients may still edit v1 records but cannot overwrite v2 as v1.

Creation accepts an `Idempotency-Key` of 1–64 characters. The actor/key pair
determines a segment UUID, and the existing creation audit stores a digest of
the input. Repeating the same request returns the segment; reusing that key for
different content returns 409 `idempotency_key_reused`. Concurrent inserts rely
on the existing primary-key constraint, not a process-local cache. No raw
idempotency key or contact data is stored in the audit digest receipt.
A retry of a subsequently deleted segment returns 409 `segment_deleted` and
cannot recreate it. Disabling v2 authoring still permits a replay that only
returns an already-created segment.

Preview keeps legacy `count`, `sample` and `excluded` meanings: eligible email
recipients for the requested purpose. Additive fields are `matched_count`,
`email_reachable_count`, `marketing_eligible_count`, `matched_sample`,
`matched_sample_scope`, `next_offset`, `definition_hash` and `evaluated_at`.
Matching someone does not grant permission to email them. The request accepts
`offset`, `limit` (1–50), `sort: "name"`, and `sample_scope` (`matches` or
`marketing_eligible`). An optional prior `definition_hash` prevents paging a
different expression. Counts are live, uncached observations; saved `last_count`
remains a timestamped no-purpose email count rather than a current total.

Campaign audience previews add `definition_hash`, `definition_version` and a
`recipient_fingerprint` over ordered contact IDs per selected channel. V2 send
requests must confirm both hashes in addition to existing channel counts.
Changes to either rules or membership return 409 `audience_changed`, including
when the numerical counts are unchanged. Queueing uses one resolved definition
for both recipient materialization and the immutable saved definition. Existing
suppression, consent, ban, event, per-channel, provider-limit and dispatch-time
checks remain authoritative.

This implementation adds no migration. Database-free regression coverage is in
`crm.tests.test_segment_trees_unit` alongside the existing segment/API/blast
suites. PostgreSQL execution plans, database-backed API/queue tests and any
subsequent expression-index migration require the repository's separately
approved database validation scope before rollout.

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
| `CRM_WAVES_ENABLED` | Stream waves and planner (default off); requires the approved 0003 schema and a live guardian |
| `LISTMONK_BASE_URL` | listmonk on the `mlai-shared` network, e.g. `http://listmonk:9000`. Never hard-coded |
| `LISTMONK_API_USER`, `LISTMONK_API_TOKEN` | The listmonk API user, sent as HTTP Basic auth. Email is `not_configured` without all three |
| `LISTMONK_TEMPLATE_ID` | The MLAI campaign template's id; blank uses listmonk's default |
| `CRM_SENDING_DOMAINS` | Comma-separated `domain:route` pairs senders may use; route is `resend` or `google_relay` (default `news.mlai.au:resend`). Bad entries are logged and skipped |
| `CRM_BLAST_DISPATCH_INTERVAL_SECONDS` | Dispatcher pass interval (default 60) |
| `CRM_LISTMONK_SYNC_INTERVAL_SECONDS` | Feedback sync interval (default 300) |

### listmonk setup (operator)

- **Version and network:** v6.2.x, pinned by digest. listmonk joins the `mlai-shared` network as `listmonk`; mlai-backend's API container (test sends) and scheduler container (both runners) must be on that network too.
- **API user** for mlai-backend with permissions to manage lists, subscribers and campaigns, send test campaigns, and run **SQL subscriber queries** (`subscribers:sql_query`). The feedback sync filters subscribers with SQL expressions; without that permission the sync fails, which stops the shared scheduler until it is fixed.
- **Template:** one MLAI-branded campaign template (`LISTMONK_TEMPLATE_ID`) with the sender identity and listmonk's unsubscribe link in the footer.
- **SMTP:** one SMTP server per sending domain, tagged with that domain (v6.2 per-domain routing). The campaign's From address picks the server, so never add two servers for one domain. Google Workspace domains use `smtp-relay.gmail.com`; Resend domains send from the domain itself, with Resend's return path on `send.` keeping SPF aligned under relaxed DMARC.
- **Privacy settings:** individual subscriber tracking on (per-person opens and clicks), `List-Unsubscribe` header with one-click unsubscribe, blocklisting allowed.
- **Bounces:** bounce processing on, with the action "blocklist" for hard bounces.
- **Senders:** add From addresses in MLAI Chat under Admin → Sending (or as a `SenderMailbox` in Django admin). Each domain in `CRM_SENDING_DOMAINS` must have its own SMTP server in listmonk whose Domains field names it; for Resend that is `smtp.resend.com:587`, user `resend`, a sending-only API key for that domain as the password.

### Domain checks (`crm/sending_domains.py`)

Admin → Sending reads `sending-domains/`, which checks each listed domain's public DNS over HTTPS (Cloudflare's resolver, then Google's) with no provider credentials. Required records decide `status`; advisory ones are shown but never fail a domain.

| Route | Required | Advisory |
| --- | --- | --- |
| `resend` | DKIM TXT at `resend._domainkey`; SPF TXT and MX at the return path `send.` (Resend's CNAME to `send.forge.rmta.net` answers both); one DMARC TXT at `_dmarc` | One SPF record on the domain; MX for replies |
| `google_relay` | DKIM TXT at `google._domainkey`; one SPF record on the domain including `_spf.google.com`; DMARC | MX for replies |

A DMARC record with `p=none` or no `rua=` passes with a note. A check whose resolvers both fail is `error` and makes the domain `unknown`; that result isn't cached.

### Composing and sending

- **Drafts:** `POST blasts/` takes `purpose`, `event_id`, `quick_segment` (`going`, `checked_in`, `no_show`, `waitlist`) or `segment_id`, `channels`, `sms_only_without_whatsapp`, and the `email`, `sms` and `whatsapp` parts. SMS and WhatsApp need an `event_update` blast with an event (400 `channel_not_allowed`). Merge tags are checked on every save (400 `unsupported_merge_tag` with `field`; event tags without an event are 400 `merge_tag_needs_event`). A draft follows its saved segment until it is queued.
- **Audience:** email follows the segment's suppression and purpose rules (`segments.evaluate`). SMS and WhatsApp count people registered for the event: SMS needs an active mobile on an allowed prefix, WhatsApp needs opt-in and a number. With `sms_only_without_whatsapp`, anyone WhatsApp reaches gets no SMS (`covered_by_whatsapp`). Each excluded person is counted once, under the first reason. SMS segments and cost are counted with merge tags rendered for each recipient.
- **Caps:** wave-enabled email may queue a campaign spanning several days; the stream's daily/hourly reservations control handoff. Legacy email uses the mailbox's rolling daily cap. SMS and WhatsApp use `CRM_SMS_DAILY_LIMIT` / `CRM_WHATSAPP_DAILY_LIMIT`.
- **`send/`** checks, in order: the blast is a draft (409 `blast_not_draft`); every channel is available (403 `channel_disabled` with `channel` and `reason`); the draft is complete (400, e.g. `mailbox_required`, `subject_required`, `sms_body_required`, `template_variable_missing`, `audience_required`); the counts equal `confirm` (409 `count_changed` with the fresh `audience/` body); and the caps (409 `daily_limit` with `limits`). It then snapshots one `BlastDelivery` per person per channel (lowercase email or E.164, with the consent basis: `event_registration`, `explicit` or `inferred_attendance`), sets `queued`, and writes a `send` audit row. The same `idempotency_key` returns the same blast; a key used by another blast is 409 `idempotency_key_reused`.
- **`test/`** sends only to the caller: email through listmonk's campaign test endpoint using separate `crm-blast-<id>-test` resources, SMS and WhatsApp through Twilio to the caller's `User.phone`. Tests never take ownership of the blast's or a wave's campaign. An unavailable channel answers 409 `channel_disabled`; no phone is 400 `test_phone_missing`.
- **`cancel/`** works on drafts, queued and sending blasts. Twilio stops between two messages. A running listmonk campaign is cancelled; its rows become `unknown`, since some may have gone. If listmonk can't be reached the blast is still cancelled and the sync retries the listmonk cancel (`last_error=listmonk_cancel_pending`).

### Runner `crm_blast_dispatch`

With `CRM_WAVES_ENABLED`, email uses `crm/deliverability/waves.py`:

- The planner preserves the existing consent/suppression resolver, excludes
  unverified/no-MX, disposable, likely-typo, prohibited role, repeatedly soft
  bounced and dormant marketing contacts, and snapshots stable ranks, activity
  tiers and provider mix. Opens never count as activity. Unanswered MX checks
  remain unknown and never change an address or its consent.
- The stream row lock reserves capacity before provider calls. The ramp starts
  at 150/day and uses Melbourne sending windows, hourly caps, provider caps,
  campaign priority and a first canary. The default canary is 50, with at least
  45 minutes before later waves (20 for an imminent event). The initial 50 is
  an explicit first-canary allowance because the nominal 150/day hourly cap
  is only 33; later waves obey the ordinary formula, without a 100-person burst.
- New lists/campaigns are `crm-blast-<id>-w<index>`. A loading reservation must
  reconcile its exact campaign before expiring. A lost start response never
  creates a replacement send. Consent and current list membership are checked
  again under contact locks immediately before start. Removed people leave
  only this list; frequency deferral does not unsubscribe them globally.
- Marketing frequency caps are three days between sends and at most two in
  seven days. Blocked rows retain rank and become `cancelled/frequency_cap`
  after seven queued days. Event updates have a separate per-event 24-hour cap.
- Staff pause/cancel intent commits before provider effects, which are retried
  safely. Owner recovery resumes the same campaign. A feature rollback still
  reconciles existing waves and prevents fresh starts.
  listmonk cannot cancel a paused campaign, so cancelling one retains its
  provider pause permanently and records local cancellation, without resuming
  it. Sync continues checking that pause and preserves campaign analytics.
- `crm_listmonk_sync` reconciles engagement per wave and aggregates outcomes
  into wave/day/blast counts. Known listmonk hard/soft bounces become deduplicated
  `EmailEvent`s; missing SMTP diagnostics are not invented. Click URLs stay
  scoped to the wave that actually sent them.
- `crm_stream_day_close` promotes at most once per day after sufficient actual
  use and healthy feedback, maintains the 30-day healthy peak, and applies
  consecutive amber-day demotion. Missing feedback is unknown and cannot
  promote. It also removes provider events older than the configured retention.

The estimate assumes healthy feedback and promotions and is refreshed by sync.
After a fully accounted-for small wave, a quiet gap is allowed; unanswered
sends require recent signed feedback. This avoids deadlocking a 50-person
canary while its next permitted slot is hours away. The full monitoring and
preflight phases remain separately gated; none of this enables live sending.

Without waves (including the finite legacy warm-up), the original dispatcher
continues as follows.

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
- **Engagement**, for 30 days after a campaign starts: opens into `first_opened_at`, clicks into `first_clicked_at` and `clicked_urls`, hard bounces into `bounced`, and list unsubscribes into `unsubscribed`. Wave soft bounces use `undelivered/soft_bounce`; only the third affected blast suppresses the address. Times are when sync first saw the event, accurate to the interval.
- **Opt-outs since the last pass:** blocklisted subscribers become `complained` (a complaint), `bounced` (a bounce) or `unsubscribed`; unsubscribes from any `crm-blast-*` list become `unsubscribed`. Each change writes an email `ContactConsentEvent` (source `listmonk:…`); unsubscribes and complaints also withdraw marketing consent; a bounce does not. A stronger status is never weakened.
- **Global blocklist:** unsubscribes are then blocklisted in listmonk, so unsubscribing means every list. Contacts opted out in MLAI Chat are blocklisted too.
- **Cursor:** the last pass time, kept in the cache and in a `sync` audit row (`target_type=listmonk`) whenever something changed. Each pass re-reads five minutes before it.

Outcomes: an outage is `deferred`; listmonk refusing the credentials or the SQL-query permission is `failed`; a blast whose campaign listmonk no longer has is skipped (`partial`).

Tests: `crm.tests.test_blasts_unit` and `crm.tests.test_listmonk_unit` run without a database; `crm.tests.test_blasts_database` runs in the CI PostgreSQL job against a fake listmonk and Twilio.

## Switching on in production

The deploy copies these from repository variables, and one secret, into the host's `.env`. An unset variable keeps the host's value, so deleting one never switches anything off: set it to `false` instead. `scripts/validate_crm_deploy_config.py` checks them before anything is copied (`tests.test_crm_deploy_config`).

| Name | Kind | Value |
| --- | --- | --- |
| `COMMUNITY_CHAT_CRM_ENABLED` | variable | `true` opens Admin → Contacts to MLAI admin and committee accounts |
| `CRM_LUMA_SYNC_ENABLED` | variable | `true` copies Luma guests; needs `LUMA_API_KEY` on the host |
| `CRM_SENDING_ENABLED`, `CRM_SMS_ENABLED`, `CRM_WHATSAPP_ENABLED` | variables | `true` switches that blast channel on |
| `LISTMONK_BASE_URL` | variable | `http://listmonk:9000` |
| `LISTMONK_API_USER` | variable | the listmonk API user's name |
| `LISTMONK_API_TOKEN` | secret | that user's token |
| `TWILIO_MESSAGING_SERVICE_SID` | variable | the Messaging Service's `MG…` SID |

A channel switched on without its credentials shows as "not configured" and sends nothing.

## Migration

`crm.0001_initial` is additive. It creates the twelve tables above and touches no existing table. Like every migration it needs explicit approval and the `APPROVED_MIGRATION_PLAN_SHA256` cycle before deployment (see `README.md`). Tests:

- `crm.tests.test_luma_guests_unit`, `crm.tests.test_luma_sync_unit` (the pass budget), `crm.tests.test_contact_import_unit`, `crm.tests.test_phones_unit`, `crm.tests.test_api_unit`, `crm.tests.test_segments_unit`, `crm.tests.test_messaging_unit`, `crm.tests.test_blasts_unit` and `crm.tests.test_listmonk_unit` run without a database.
- `crm.tests.test_contact_store_database`, `crm.tests.test_api_database`, `crm.tests.test_segments_database`, `crm.tests.test_messaging_database` and `crm.tests.test_blasts_database` run in the CI PostgreSQL job.

Legacy blasts use `crm.0001_initial`. Stream/event/incident foundations use
`crm.0002_deliverability_foundations`; waves, day budgets and domain cache use
`crm.0003_campaign_waves_and_planner`. Creation and synthetic disposable tests
of 0002/0003 were approved separately. Production migration and enablement
remain unapproved. See [the implementation record](crm-deliverability-implementation.md)
and [the exact Phase 2 schema](crm-deliverability-phase-2-schema.md).

## Ownership

| | |
| --- | --- |
| Owning package | `crm` |
| Entry points | the scheduler runners (`crm_luma_guest_sync`, `crm_blast_dispatch`, `crm_listmonk_sync`), the management commands, the contacts API under `/api/v1/community-chat/crm/`, and the Twilio webhooks under `/api/v1/crm/webhooks/` |
| Personal data | names, emails, phone numbers, employers and registration answers |
| Retention | retention undecided |

### Multiple source calendars

The configured community Luma key remains a source. Additional calendars must be
explicitly selected by an MLAI admin in Connections. Their encrypted credentials
remain account-scoped. Scheduled CRM ingestion rechecks the selecting admin's
active authority, unions listings by Luma event ID and reads one complete guest
list only through a currently selected source with explicit manage access.
External/view-only listings remain visible with unavailable guest data. Failed
catalogue reads do not replace a complete catalogue with partial results.

CRM ingestion uses normalized-email contact deduplication and event-ID activity
provenance. Account aggregation never sends mail or updates marketing/WhatsApp
consent. Preferences, unsubscribes and holds remain independent of engagement.
Selecting CRM calendars does not combine financial metrics or alter report
account choices. A calendar API key does not discover all events hosted by a
person outside that calendar; a separately authenticated host source is needed.

## Campaign stream foundations (one-click plan, Phase 1)

See [the audit, exact schema proposal and phase gates](crm-deliverability-implementation.md).
The schema requires explicit migration approval before creation or application.
A production rollout also requires the source plan's Phase 0 provider/DNS/SMTP
checks; code and synthetic tests do not prove those checks passed.

- `CRM_STREAMS` maps purpose to identity. Default `news.mlai.au` serves
  marketing and event updates; `notify.mlai.au` is transactional. CRM blasts
  refuse transactional streams. Staff draft saves and queueing choose the
  stream's mailbox; old clients' mailbox IDs are ignored for staff.
- The root `mlai.au` is always protected, even if omitted from
  `CRM_PROTECTED_DOMAINS`; that setting may protect additional exact domains.
  Mailbox writes return 400 `mailbox_domain_protected`, direct root campaign
  queueing returns 409 `protected_domain`, and dispatch refuses root starts.
- `crm_streams sync` creates the configured stream records and disabled
  mailboxes idempotently. It never enables mailboxes, resets incidents or
  ramp state, or overwrites an existing identity that differs. Run it only
  against an approved schema/environment. It does not call any provider.
- `CRM_DELIVERABILITY_OWNERS` defaults to `tech@mlai.au`. Owners must also
  pass the existing MLAI admin and verified installation gates. Mailbox
  writes and incident acknowledgement require owner capability.
- `GET access/` includes `can_manage_deliverability`. Owner-only routes are
  `GET deliverability/`, `GET deliverability/incidents/?state=`,
  `GET deliverability/incidents/{id}/` and
  `POST deliverability/incidents/{id}/acknowledge/`.
- Acknowledgement accepts `resolution` (`resume_slow`, `resume`, `stop_blast`,
  `keep_stopped`) and optional `note`. Recovery refuses other open red
  incidents, disabled sending or unverified domain checks. It demotes the
  ramp two stages; `resume_slow` records a 48-hour recovery window. Staff
  pauses remain paused. The owner decision commits a pending recovery before
  any provider restart; the dispatcher rechecks admission and resumes the same
  campaign. Timeouts or lost database acknowledgements reconcile the existing
  campaign state without creating another send. Unchanged acknowledged evidence does not reopen an
  incident; new negative evidence can. Every choice is audited.
- `CRM_DELIVERABILITY_SLACK_CHANNEL` enables aggregate-only alerts and
  four-hour red reminders. No recipient addresses appear in these alerts.
  Transactional email alerts, full monitors and the health UI follow in
  Phase 5. Phase 2 implements the volume effect of slowed/recovery state;
  the Phase 1 flag must remain off until its release gates are approved.
- Numeric guard defaults live only in `crm/deliverability/policy.py` and are
  read through settings. The deploy validator, workflow and host upsert
  inventory carry the same keys; empty release values preserve host values.

Database-free acceptance: `scripts/test_without_database.py crm.tests.test_deliverability_unit`.
Synthetic database acceptance (specific migration approval first):
`manage.py test crm.tests.test_deliverability_database`.

## Campaign preflight (Phase 3)

`CRM_PREFLIGHT_ENABLED` is false by default. With it enabled, email draft
checks bind the message, event substitutions, actual stream identity, template
ID and template bytes to a SHA-256 hash. Send requires that hash, checks
younger than 24 hours, no blocking items and fresh provider configuration.
Draft/content/template/policy changes require another check. Campaigns already
queued may span days; their content and template must still match before each
new wave. Staff can queue while the inbox check runs; no audience wave starts
until it completes without a block or reaches the timeout. The finite pilot
retains its separately constrained path.

Verified admins use `POST blasts/{id}/preflight/`, poll the same route with
GET, and use `POST blasts/{id}/preflight/fix/` with `expand_short_links`,
`upgrade_https`, `add_preview_text` or `remove_subject_prefix`. Fixes apply
only to drafts, detect competing edits, clear old results, and are audited.
Draft saves accept `preview_text` (200 characters) and `sign_off_name` (120).
`POST blasts/{id}/send/` accepts `preflight_hash`. General staff responses
omit owner diagnostics and seed mailbox identities; every response is no-store.

Checks cover message content, bounded public link inspection, list/template
configuration, audience exclusions/timing/frequency/duplicates, recent delivery
feedback, reputation and Postmaster evidence. Unknown reputation and missing or
stale Postmaster data remain warnings. A known listing, invalid identity,
unsigned one-click unsubscribe or unavailable spam score blocks. DNS is
validated again on every redirect and connections are pinned to the validated
public IP, with TLS checked against the original hostname. Rendered previews
use private empty resources and never load or send the intended audience.

`ops/spamcheck/` prepares the internal Rspamd content service. It is not
automatically started by the backend release. Its runtime image/profile and
real seed delivery remain staging gates, not results established by fixtures.
The content scan does not pretend to authenticate an unsigned pre-send MIME.

Dedicated seeds are enrolled explicitly with `crm_seed_mailboxes` from
`CRM_SEED_MAILBOXES` JSON. That command neither reads secrets nor sends mail.
Rows store only `CRM_SEED_*` environment-variable references; `xoauth2` expects
an access token maintained by the host secret manager. Enabled seed addresses
are the only recipients of the separate listmonk test call. CRM contact status,
consent and audience lists are not modified. An immutable message snapshot is
retained for recovery; a timed-out/uncertain test submission is never repeated
blindly. Polling is read-only IMAP with special-use folders and Gmail category
search. Receiver-added authentication must prove an aligned DKIM signature
covering both one-click headers in the same passing signature.

Two distinct major providers reporting spam blocks; one warns. At 15 minutes,
missing results warn, create an amber incident, and halve only the first
canary with its matching sample threshold. The configured 45-minute wait and
all later stream capacity limits remain in force. Synthetic tests cover every
check, binding/config changes, fixes, seed recovery, authenticated header
proofs, timeout pacing and migration 0004 reversal/reapplication. Real inbox
access and real seed/campaign sends are excluded from the local test approval.
