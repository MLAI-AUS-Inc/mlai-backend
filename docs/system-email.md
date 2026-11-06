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

Templates live in `system_email/templates/system_email/`. The four
notification messages above `monthly_*` use the subject and body their
callers already build.

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
