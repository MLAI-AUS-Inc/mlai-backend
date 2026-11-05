# listmonk email delivery

This Compose project runs [listmonk](https://listmonk.app) v6.2 and its own
PostgreSQL database, separate from `mlai-backend`. It is Phase 0 of the
contacts and blasts plan (`mlai-chat/docs/mlai/CONTACTS_EVENTS_EMAIL_PLAN_2026-10-23.md`).

listmonk only **delivers email blasts**:
- `mlai-backend` (the `crm` app) is the source of truth for contacts, consent and blasts. Wave-enabled sending creates one list/campaign per reserved batch; legacy sending keeps one per blast. It reads opens, clicks, unsubscribes and bounces back every few minutes.
- SMS and WhatsApp blasts don't touch listmonk. `mlai-backend` sends them through Twilio (see [Twilio](#twilio-sms-and-whatsapp)).

Don't add, edit or delete subscribers, lists or campaigns in the listmonk UI. `mlai-backend` manages them, and unsubscribes made in listmonk are copied back to the contact in MLAI.

## Addresses

| Setting | Consumer | Meaning |
| --- | --- | --- |
| `LISTMONK_BASE_URL=http://listmonk:9000` | `mlai-backend` only | Private API on the shared Docker network |
| `https://lists.mlai.au` | Email recipients | Public root URL for unsubscribe pages, "view in browser", tracked links and the open pixel |
| `http://127.0.0.1:9100/admin` | Operators | Admin UI, through an SSH tunnel only |

Open the admin UI with `ssh -L 9100:127.0.0.1:9100 mlai-droplet`, then browse to `http://localhost:9100/admin`.

## Invariants

- **PostgreSQL** is only on the internal Compose network.
- **listmonk** binds its admin UI and API to host loopback (port 9100). It joins `mlai-shared` as `listmonk`, which also gives it the outbound SMTP access it needs.
- **The public ingress** ([`nginx-public-cloudflare-proxied.conf`](./nginx-public-cloudflare-proxied.conf)) allowlists only the subscriber routes: `/subscription/*` (except the open signup form), `/link/*`, `/campaign/*`, `/public/*`, `/uploads/*` and `/robots.txt`. `/admin`, `/api` (including `/api/public`), `/archive` and everything else get a fail-closed 404.
- **No access log** is kept for the public hostname. Every allowed path carries a subscriber UUID, so a log would tie recipients to IP addresses.
- **The backend's API user** has only the permissions listed in [step 8](#first-deployment).
- **The image is pinned by digest.** listmonk migrates its schema when it starts, so an image change is an upgrade (see [Upgrades](#upgrades)).

## First deployment

On the droplet, run this project from `/root/mlai-listmonk`, next to `/root/mlai-content-analytics`. Keep it out of `/root/mlai-backend`, because `deploy.sh` rsyncs that directory with `--delete` on every backend deploy.

1. **Environment.** Copy `.env.example` to `.env` and `chmod 600 .env`.
   - Generate `LISTMONK_POSTGRES_PASSWORD` with `openssl rand -hex 32`. It must stay URL-safe.
   - Leave `LISTMONK_ADMIN_USER` and `LISTMONK_ADMIN_PASSWORD` empty. listmonk then starts with no users, and the first visit to the admin UI asks for the super admin's name and password (step 5), so the password never sits in a file. Only someone with an SSH tunnel can reach that page.
2. **Images.** Keep both image values as immutable `tag@sha256` references. Compose has no `latest` fallback.
3. **Shared network.** The `mlai-shared` network already exists on the droplet, because `deploy.sh` creates it. Elsewhere, run `docker network create mlai-shared`.
4. **Validate and start:**

   ```sh
   docker compose --env-file .env config >/dev/null
   docker compose up -d database
   docker compose up -d listmonk backup
   docker compose logs --tail=100 listmonk
   curl -fsS http://127.0.0.1:9100/health
   ```

5. **Create and secure the admin account.** Open the admin UI over the SSH tunnel straight away and create the super admin, then turn on two-factor authentication for it. If you set `LISTMONK_ADMIN_*` instead, delete both lines from `.env` after the first sign-in.
6. **Settings** (Admin → Settings):

   | Section | Setting | Value |
   | --- | --- | --- |
   | General | Root URL | `https://lists.mlai.au` |
   | General | Site name | `MLAI` |
   | General | Default "from" address | The bulk sender, e.g. `MLAI <hello@events.mlai.au>`. `mlai-backend` sets each blast's From itself. |
   | Privacy | Individual subscriber tracking | On (decided 23 Oct 2026; MLAI's privacy policy must mention open and click tracking) |
   | Privacy | Include `List-Unsubscribe` header | On. It adds the one-click `List-Unsubscribe-Post` header that Gmail and Yahoo require for bulk mail. |
   | Privacy | Allow blocklisting | On |
   | Privacy | Allow preferences, exporting, wiping | Off. Lists here are per blast, and data requests are handled in MLAI, so a wipe here would leave the MLAI contact behind. |
   | Performance | Concurrency, message rate, sliding window | Start at concurrency 5 and a message rate under the slowest provider's limit (2 per second suits a default Resend account). Add a sliding window, e.g. 1,000 per hour, while warming up. |
   | Media | Provider | Filesystem, upload path `/listmonk/uploads`, URI `/uploads` |
   | Bounces | Enable bounce processing | On. Hard bounces and complaints: blocklist. Soft bounces: none. |

   On bounces, also:
   - with SES, enable its bounce webhook and uncomment `/webhooks/service/ses` in the nginx file;
   - Resend has no listmonk bounce webhook; Resend suppresses bounced addresses itself;
   - mail sent through Google's relay bounces back to the sending mailbox, and listmonk can scan one POP mailbox for those.
7. **SMTP servers** (Admin → Settings → SMTP). Add **one server per sending domain**, and put that domain in the server's **Domains** field. Since v6.2, listmonk picks the server whose domain matches the From address. Two servers on one domain are picked at random, so never add two.
   - **Other verified sending domains, through Google's SMTP relay.** Protected root domains cannot be campaign From addresses.
     - In the Google Admin console, open Apps → Google Workspace → Gmail → Routing → SMTP relay service, and add a rule:
       - allowed senders: **only addresses in my domains**;
       - **require SMTP authentication**;
       - **require TLS encryption**;
       - optionally, only the droplet's IP.
     - In listmonk: host `smtp-relay.gmail.com`, port 587, TLS `STARTTLS`, auth `login`. The username is a Workspace user's address; the password is that user's app password (2-Step Verification must be on).
     - Read the relay's daily limits off the admin console. Google publishes about 10,000 recipients per user per day, plus a customer-wide limit that scales with licence count. Keep each From address's `daily_limit` in MLAI under them.
   - **Bulk sends, through Resend on a dedicated subdomain** (for example `events.mlai.au`).
     - In Resend, verify the subdomain by adding the SPF, DKIM and return-path records it shows. Create an API key with sending access restricted to that domain.
     - In listmonk: host `smtp.resend.com`, port 587 `STARTTLS` (or 465 TLS), username `resend`, password the API key, Domains `events.mlai.au`.
     - Before the first big send (about 25,000 people), ask Resend to raise the account's rate limit. Then raise listmonk's message rate to match.
   - **Check each server.** Use "Test connection", then send a test campaign from each domain to Gmail and Outlook seed inboxes. Check the headers show SPF, DKIM and DMARC `pass`, plus `List-Unsubscribe-Post`.
8. **API user for `mlai-backend`.**
   - Under Users, create a role named `mlai-backend` with only these permissions:
     - `lists:get_all`, `lists:manage_all`;
     - `subscribers:get`, `subscribers:get_all`, `subscribers:manage`, `subscribers:sql_query`;
     - `campaigns:get_all`, `campaigns:get_analytics`, `campaigns:manage_all`, `campaigns:send`;
     - `templates:get`, `bounces:get`.
   - Create an **API** user with that role and copy its token. `subscribers:sql_query` is required: without it the feedback sync gets 403 and stops the shared scheduler.
9. **Template.**
   - Create a campaign template named `MLAI blast` that wraps `{{ template "content" . }}`.
   - Its footer must identify MLAI (name and a contact address) and include `{{ UnsubscribeURL }}`. Add `{{ TrackView }}` for the open pixel.
   - Note its ID.
10. **Public hostname.**
    - Add a proxied Cloudflare `A` record for `lists.mlai.au` pointing at the droplet, and set a configuration rule for SSL mode "Full".
    - Create the self-signed origin certificate (command at the top of the nginx file) and install the file as `/etc/nginx/conf.d/listmonk-public.conf`.
    - Then run:

      ```sh
      nginx -t && systemctl reload nginx
      # The origin itself, before or without Cloudflare:
      PUBLIC_LISTMONK_ORIGIN=https://lists.mlai.au ORIGIN_IP=127.0.0.1 LOCAL_LISTMONK_URL=http://127.0.0.1:9100 ./smoke-test.sh
      # Through Cloudflare, once the DNS record exists:
      PUBLIC_LISTMONK_ORIGIN=https://lists.mlai.au LOCAL_LISTMONK_URL=http://127.0.0.1:9100 ./smoke-test.sh
      ```

11. **Connect `mlai-backend`.** In `/root/mlai-backend/.env`, set:
    - `LISTMONK_BASE_URL=http://listmonk:9000`;
    - `LISTMONK_API_USER` and `LISTMONK_API_TOKEN` (from step 8);
    - `LISTMONK_TEMPLATE_ID` (from step 9).

    Leave `CRM_SENDING_ENABLED=false` until a test blast to an internal MLAI-team segment has worked end to end. These keys are not pushed by `deploy.sh`, so add them by hand. The backend side is described in `mlai-backend` `docs/crm.md` (on the contacts API branch until MLAI-AUS-Inc/mlai-backend#1198 merges).

## DNS for every sending domain

| Record | Value |
| --- | --- |
| SPF | `TXT @ "v=spf1 include:_spf.google.com ~all"` for Workspace domains. Resend publishes its own SPF on its return-path subdomain. Only one SPF record per name. |
| DKIM | **Workspace:** Admin console → Apps → Gmail → Authenticate email → generate the key and publish the TXT record, then "Start authentication". **Resend:** the records it shows. |
| DMARC | `TXT _dmarc "v=DMARC1; p=none; rua=mailto:<reports address>"`. Tighten to `quarantine` once reports are clean. |

Gmail and Yahoo treat a sender of more than 5,000 messages a day as a bulk sender. That requires:
- SPF and DKIM on every sending domain, plus an aligned DMARC record;
- one-click unsubscribe;
- unsubscribes honoured within two days;
- spam complaints under 0.3%.

Warm up a new domain or subdomain over about two weeks, sending to recent attendees first.

## Twilio (SMS and WhatsApp)

These steps don't involve listmonk. `mlai-backend` sends SMS and WhatsApp itself.

1. In the Twilio console, confirm the WhatsApp-linked number is **SMS-capable**. If it isn't, buy an Australian mobile number for SMS.
2. **Messaging Service** (Messaging → Services):
   - Create `MLAI blasts` and add the number as a sender.
   - Turn on **Advanced Opt-Out** with the default keywords plus `PAUSE`.
   - Under Integration → incoming messages, choose "Send a webhook": `POST https://api.mlai.au/api/v1/crm/webhooks/twilio/sms-inbound/`.
   - Delivery status callbacks are set on each message by `mlai-backend` (`…/api/v1/crm/webhooks/twilio/status/`), so they need no console setting.
3. **WhatsApp sender.** Leave its incoming-message URL on the existing research-automation webhook (`…/notification-webhooks/whatsapp/`). `mlai-backend` passes WhatsApp STOP, START and JOIN, and replies, on to `crm` from there.
4. **WhatsApp templates** (Content Template Builder). Submit **Utility** templates for WhatsApp approval, keeping fixed wording around the variables, because Meta rejects templates that are mostly variables. Starters:
   - `mlai_event_reminder`: "Hi {{1}}, a reminder that {{2}} starts {{3}}. Details: {{4}}. Reply STOP to opt out."
   - `mlai_event_change`: "Hi {{1}}, an update about {{2}}: {{3}}. Reply STOP to opt out."
   - `mlai_event_thanks`: "Thanks for coming to {{2}}, {{1}}! Here's the follow-up: {{3}}. Reply STOP to opt out."
5. **`mlai-backend` settings.** In its `.env`, set:
   - `TWILIO_MESSAGING_SERVICE_SID`;
   - `CRM_SMS_ALLOWED_PREFIXES=+614`;
   - `CRM_SMS_COST_PER_SEGMENT_AUD` and `CRM_WHATSAPP_COST_PER_MESSAGE_AUD`, from current Twilio and Meta pricing;
   - `CRM_SMS_DAILY_LIMIT` and `CRM_WHATSAPP_DAILY_LIMIT`.

   Then load the approved templates:

   ```sh
   docker compose exec -T web python manage.py crm_sync_whatsapp_templates
   ```

   In Django admin → Message templates, map each variable to a source (first name, event name, start time, event URL or custom text) and tick **Enabled**. Leave `CRM_SMS_ENABLED` and `CRM_WHATSAPP_ENABLED` off until a test blast to the MLAI team has worked.
6. **Sender name.** SMS goes from the Twilio mobile number. A numeric sender like this is not affected by the ACMA SMS Sender ID Register, which has labelled unregistered alphanumeric sender names "Unverified" since 1 July 2026. Use an alphanumeric "MLAI" sender only after registering it through Twilio.

## Backups and restore

**What runs.** `backup` writes a compressed custom-format dump at start and then every 24 hours. Incomplete dumps keep a `.partial` suffix. Dumps are kept for 30 days by default.

**What isn't covered.** The Docker volumes are not an off-machine backup:
- Replicate `listmonk-postgres-backups` to encrypted, access-controlled storage.
- Also copy `listmonk-uploads` (template images): `docker run --rm -v mlai-listmonk_listmonk-uploads:/u -v "$PWD":/out alpine tar czf /out/listmonk-uploads.tgz -C /u .`
- Alert when `backup` is unhealthy.

**List and check the latest dump:**

```sh
docker compose run --rm --entrypoint sh backup -c \
  'ls -lh /backups/listmonk-*.dump && pg_restore --list "$(ls -1t /backups/listmonk-*.dump | head -1)" >/dev/null'
```

**Restore.** This **replaces the current listmonk database**. Rehearse it in an isolated project first, and in a real incident stop sending (`CRM_SENDING_ENABLED=false` in `mlai-backend`):

```sh
docker compose stop listmonk backup
docker compose exec database sh -ec \
  'dropdb --force -U "$POSTGRES_USER" "$POSTGRES_DB"; createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T database sh -ec \
  'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /secure/path/listmonk-YYYYMMDDTHHMMSSZ.dump
docker compose up -d listmonk backup
```

Losing listmonk data loses delivery history and per-person opens and clicks for recent blasts. Contacts and consent live in `mlai-backend`.

## Upgrades

listmonk runs `--upgrade` on every start, so changing `LISTMONK_IMAGE` migrates the schema immediately. For each upgrade:

1. Read the listmonk release notes, especially for API and settings changes.
2. Take and check a fresh dump.
3. Test the exact new digest in staging, against the `crm` feedback sync and a test blast.
4. Set `CRM_SENDING_ENABLED=false`, change `LISTMONK_IMAGE`, run `docker compose up -d listmonk`, and read its logs.
5. Run the smoke test and a test blast, then turn sending back on.

A rollback means restoring the pre-upgrade dump with the previous digest, not just changing the tag back.

## Smoke test

[`smoke-test.sh`](./smoke-test.sh) checks two things:
- the public hostname serves static assets and reaches listmonk on the subscriber routes;
- `/`, `/admin`, `/api`, the public signup API and form, the archive and the admin bounce webhook are closed.

With `LOCAL_LISTMONK_URL` set, it also checks listmonk's health on loopback. With `ORIGIN_IP` set, it skips DNS and Cloudflare and talks to nginx on that address, trusting only the origin certificate in `ORIGIN_CA` (default `/etc/nginx/ssl/listmonk-selfsigned.crt`).

```sh
PUBLIC_LISTMONK_ORIGIN=https://lists.mlai.au LOCAL_LISTMONK_URL=http://127.0.0.1:9100 ./smoke-test.sh
```

References:
- [listmonk configuration and HTTP routes](https://listmonk.app/docs/configuration/)
- [installation](https://listmonk.app/docs/installation/)
- [roles and permissions](https://listmonk.app/docs/roles-and-permissions/)

## One-click campaign streams: Phase 1 rollout gate

The backend now prepares protected stream identities and rate-based feedback
protection. This is a phased implementation; see
[`docs/crm-deliverability-implementation.md`](../../docs/crm-deliverability-implementation.md).
The existing listmonk instance and finite pilot must be preserved. Do not
restart or recreate listmonk as part of this code change.

Before any production enablement, verify the source plan's Phase 0 checklist:

1. Verify `news.mlai.au` and `notify.mlai.au` with provider-issued DNS records,
   separate scoped sending keys, and a decision on separate marketing-team
   suppression. Do not infer provider account quota from the ramp ladder.
2. Add the news SMTP domain route; retain the pilot's existing routes. Keep
   account/day/month limits and the pilot's reserved capacity intact.
3. Check the public root URL, unsubscribe headers and signed one-click
   headers using real seed headers. Enable `settings:get` on the backend role
   for the later config monitor. The template must identify MLAI, give a
   contact address, include `{{ UnsubscribeURL }}`, and print the per-recipient
   `{{ .Subscriber.Attribs.consent_reason }}` written by wave loading.
4. Subscribe the signed callback to all seven documented delivery events for
   stream and system domains. Verify real signed simulator feedback. Storing
   an event is not proof that a message reached the inbox.
5. After the exact schema migration is separately approved, sync stream
   records. New mailboxes stay disabled. Verify the owner list and alert
   channel before enabling the guardian. Dry-run is staging only.
6. Inspect Postmaster, DMARC reporting, reputation and seed prerequisites.
   Do not tighten DMARC or switch transactional identities in this phase.

Phase 1's callback records feedback and suppresses individual bad addresses;
it never disables the two pilot senders after one event. The finite pilot
still applies its own admission/stop policy through its scheduler. The new
guardian pauses campaign blasts only on sample-qualified rates or repeated
policy rejections; marketing incidents never act on the notify stream.
Owner acknowledgement is required for red recovery. Provider pause failures
remain durably blocked and retry on the next guardian pass.

## Campaign waves: Phase 2 rollout gate

`CRM_WAVES_ENABLED` stays false until schema and acceptance gates are reviewed.
The backend owns `crm-blast-<uuid>-w<index>` resources and separate
`crm-blast-<uuid>-test` resources. Never edit their lists or restart a completed
campaign manually. Unknown starts are reconciled by exact name and retained ID.
When staff cancel a paused wave, it remains paused in listmonk and is locally
cancelled. listmonk only allows a running campaign to transition to cancelled;
the backend never resumes a paused campaign just to satisfy that restriction.

Migration 0003 adopts live campaign IDs into wave zero without calling this
service or changing consent, status or recipients. Existing running campaigns
can finish through their original resources. This is not permission to restart
listmonk, migrate production, extend the finite pilot, or enable a sender.

The backend's stream/day/hour allowances are the primary pacing control.
Keep listmonk's account rate and sliding window as provider-aligned backstops;
do not raise them from a theoretical ramp maximum without verifying the
account's current rate and quota. The generated campaign supplies fixed MLAI
From identity, Reply-To, readable `altbody`, X-MLAI-Blast, X-MLAI-Wave and
Feedback-ID. Actual DKIM coverage of those headers and one-click unsubscribe
still needs real seed-header acceptance before production rollout.

## Preflight resources and Phase 3 gate

Preflight owns empty `crm-blast-<uuid>-preflight-<content-hash>` resources and
seed checks own `crm-seed-<uuid>` resources. They are separate from campaign
waves and manual tests. Never start a preflight or seed campaign through the
UI: only listmonk's test endpoint delivers the dedicated seed snapshot. The
backend commits a submission marker before the test call, then polls after an
uncertain response rather than risking a second submission.

Enable `settings:get` for the backend role. Read-only checks require the exact
news SMTP `from_addresses` route, verified STARTTLS/TLS, unsubscribe headers,
the public lists URL, bounded account message rate and permanent-bounce/
complaint blocklisting. Temporary bounces are managed by the backend streak
policy and must not be globally blocklisted after a single occurrence. The
template must include MLAI, its contact address, `{{ UnsubscribeURL }}` and
the consent-reason attribute. Provider settings responses and masked SMTP
credentials are never retained in preflight results.

The [internal content checker](../spamcheck/README.md) is prepared separately.
Keep `CRM_PREFLIGHT_ENABLED=false` until the selected scanner image has passed
configuration/scoring fixtures and an authorized staging send proves actual
Gmail/Outlook/Yahoo placement and signed one-click headers. Neither these files
nor the disposable test approval authorize starting this stack, changing live
listmonk settings, real inbox access or sending a seed message.
