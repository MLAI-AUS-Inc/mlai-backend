# System email (transactional mail through Resend)

The `system_email` app sends the email MLAI's own code sends to one person:
sign-in codes, magic links, password resets, notification-channel checks,
article notifications, performance briefs and monthly-update reminders. It
replaces Customer.io one message at a time.

It is separate from CRM sending on purpose. It never uses `SenderMailbox`,
warm-up caps, waves or the CRM guardian, and it has its own Resend key, From
domain (`notify.mlai.au`) and webhook. A CRM bounce never pauses it, and its
bounces never pause CRM senders (the CRM webhook ignores senders outside
`CRM_RESEND_MONITORED_SENDERS`).

## Message keys

| Key | Email | Caller |
|---|---|---|
| `chat_sign_in_code` | MLAI Chat six-digit code | `community_chat/email_delivery.py` |
| `magic_link` | Sign-in link (all apps) | `core/email_utils.send_magic_link_email` |
| `password_reset` | Password setup/reset link | `core/email_utils.send_password_reset_email` |
| `notification_channel_verify` | "Confirm your notification email" | `integrations/services/notification_channels.py` |
| `article_review_ready` | "Your article is ready to review" | `notification_adapters._send_channel_delivery` |
| `article_content_ready` | "Article ready for {domain}" | same |
| `article_report_ready` | Article performance brief | `content_analytics/services/report_notifications.py` |
| `monthly_update_7d`, `monthly_update_1d` | Coworking-rate reminders | `startup_updates/monthly_update_reminders.py` |
| `mentor_invite` | "{Name} invited you to mentor at MLAI" | `mentoring/invite_email.py` (Mentoring admin) |

`mentor_invite` has no legacy provider and is not listed in
`SYSTEM_EMAIL_RESEND_MESSAGES`: it always goes through Resend, and only while
`MENTOR_INVITE_EMAIL_ENABLED` is true. It is laid out like the CRM event invite
(`crm/email_layouts.py`) and uses the same hosted icons
(`CRM_EMAIL_ASSET_BASE_URL`). Replies go to the staff member who sent it
(`send(..., reply_to=...)`), not `SYSTEM_EMAIL_REPLY_TO`. Test it with
`python manage.py send_system_email_test mentor_invite` (sample context in
`system_email/fixtures.py`, dummy link).

Templates live in `system_email/templates/system_email/`. The shared
transactional layout provides the Roo mark, MLAI wordmark, mobile-width
layout and a labelled action when the recipient needs one. Keep codes as
selectable text. For emails built in the calling application, pass the HTML
from `system_email.layouts.render_transactional_email` alongside the existing
plain-text body. Pass plain strings as paragraphs and use safe HTML only for
already escaped markup or a separately rendered template. Keep email
preferences and unsubscribe links tied to the caller's existing controls.
Mentor invitations, mentor feedback and startup invitations use their
dedicated layouts.

Preview templates with synthetic inputs and run
`python scripts/test_without_database.py system_email.tests.test_system_email_unit`.
Before releasing an HTML change, check for unresolved send attempts on
affected keys. Resend expects the same payload when reusing an idempotency
key. Let in-flight system emails finish on the current release, and
reconcile uncertain deliveries before retrying them across the rollout.
Founder transition retries with a checkpointed payload retain that payload.

## Switching a message over (and back)

`SYSTEM_EMAIL_RESEND_MESSAGES` lists the keys sent through Resend, or `all`.
Keys not listed keep their old provider. Rolling back is removing the key and
restarting the web app and the email workers. Recommended order: the
notification keys first, then after a clean week `password_reset`,
`magic_link`, and last `chat_sign_in_code`, a day apart.

Before the first key: create `notify.mlai.au` in Resend with open/click
tracking off, a sending-only API key for that domain
(`SYSTEM_EMAIL_RESEND_API_KEY`), and a webhook to
`/api/v1/system-email/webhooks/resend/` (secret in
`SYSTEM_EMAIL_RESEND_WEBHOOK_SECRET`). `validate_prod_urls` refuses to start
production with keys routed but these unset, or with the CRM key reused.

Production values come from GitHub: secrets `SYSTEM_EMAIL_RESEND_API_KEY` and
`SYSTEM_EMAIL_RESEND_WEBHOOK_SECRET`, and repository variables
`SYSTEM_EMAIL_RESEND_MESSAGES`, `MLAI_SYSTEM_FROM_EMAIL`, `SYSTEM_EMAIL_REPLY_TO`,
`SYSTEM_EMAIL_UNSUBSCRIBE_MAILTO`, `SYSTEM_EMAIL_TEST_RECIPIENTS` and
`SYSTEM_EMAIL_ALERT_SLACK_CHANNEL_ID`. `deploy.sh` copies them to the host on
each deploy. `SYSTEM_EMAIL_RESEND_MESSAGES` is always copied, so clearing the
variable and redeploying rolls every message back; the others keep the host's
value when empty.

Check a template end to end with
`python manage.py send_system_email_test --message <key> --to <address>`;
the address must be in `SYSTEM_EMAIL_TEST_RECIPIENTS`.

## Monthly reminders held for review

With `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=true` (the default), reminders on
Resend stop at status `drafted` with `held_for_review` in the provider
response. In Django admin, Monthly update reminder deliveries, select them and
run **Send held reminders (Resend)**.

## Records, bounces and alerts

- `SystemEmail` rows log each message (key, recipient, status, Resend id). They
  never contain the body, a code or a link.
- Hard bounces and spam complaints add a `SystemEmailSuppression`. Optional
  mail to a suppressed address is not sent; sign-in mail is still attempted so
  support can see the failure. Clear a suppression from Django admin (and in
  Resend if it suppressed the address too).
- `python manage.py system_email_health [--loop-seconds 300]` posts to
  `SYSTEM_EMAIL_ALERT_SLACK_CHANNEL_ID` when more than 5 % of a category fails
  in 15 minutes (at least 5 sends), p95 delivery passes 2 minutes, or sign-in
  mail gets a complaint. It never pauses sending.

Build plan: project file `plans/customerio-to-resend-transactional.md`.

### Startup team invitations

`startup_team_invite` uses the mentor invitation's layout with the inviter,
the selected startup and its uploaded branding. MLAI's Roo mark and wordmark
appear with or without a company logo. The HTML and plain-text templates are
versioned as `startup_team_invite_v1` to keep queued retries consistent.
The acceptance reference in the URL requires the recipient's verified MLAI
account.

For a preview without sending, call `system_email.render.render` with
`get_spec("startup_team_invite")` and the matching entry in
`system_email.fixtures.SAMPLE_CONTEXT`. Check a version with a synthetic logo
and one with the logo omitted. The existing `send_system_email_test` command
can send the registered sample to a configured test recipient when authorized.
