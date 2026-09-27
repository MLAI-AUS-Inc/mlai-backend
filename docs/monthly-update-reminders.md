# Monthly update renewal reminders

The existing `scheduler` service invokes `run_scheduled_discovery` every minute. That command now also ticks the monthly-update reminder runner. Email is gated by `MONTHLY_UPDATE_REMINDERS_ENABLED=true`; Roo chat is independently gated by `MONTHLY_UPDATE_ROO_REMINDERS_ENABLED=true`. Both are off by default and only select reminders after the configured Melbourne send time.

## Eligibility and timing

A reminder is due only when all of these remain true at dispatch time:

- the user is active and has an email address;
- the user owns a registered Australian startup with a checksum-valid, ABR-verified ABN (an ACN is optional, so incorporated nonprofits such as MLAI qualify);
- the user has a discount-eligible startup binding to the same organization; and
- the organization's latest approved monthly update is exactly seven days or one day from expiry.

The benefit expires exactly 30 days after the first immutable human approval timestamp. Older generated `ready_at` timestamps cannot start the clock early when an approval record exists. Recreated drafts retain the earlier monthly payment’s window through the durable reward ledger. Generating a draft does not qualify, and editing/reapproving the same month does not restart the clock. Existing approved updates retain eligibility while newer edits are in draft. The one-day reminder runs on the Melbourne calendar day before expiry; email additionally supports seven days before expiry. Payloads include the exact expiry timestamp and display time, including daylight saving; templates render `expires_at_display`. Legacy `valid_through` fields denote the last date with any eligible time (the prior day for a midnight expiry), not all-day eligibility. There is no historical catch-up, so enabling the feature cannot send old reminders.

## Roo chat

One day before expiry, Public Roo sends the founder a direct message with the exact due time, the 20-point monthly reward, the 30-day 4-point coworking benefit, and a company-scoped `COMMUNITY_CHAT_FRONTEND_URL/pulse?startup=<company_uuid>&startupView=new` link that opens the selected startup’s monthly-update composer. It uses the existing private Slack mirror. The owner must have an active grant and connected Slack account; disconnected/paused/revoked owners are skipped. There is no email-based identity matching or fallback to a different recipient.

The scheduler verifies the bot token with Slack `auth.test` against the configured Public Roo workspace/user identity before posting. The Slack mirror continues to enforce its normal owner and conversation privacy boundaries. No new Roo service or native bot identity is needed. Customer.io credentials/templates are not required for chat, and `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT` applies only to email.

The existing delivery row stores independent chat state under `provider_response.roo_chat`, preserving email receipts. A row lock and claim prevent concurrent sends. Safe preflight failures and explicit rate-limit rejections retry with exponential backoff (at most five attempts on the due day); expired preparation leases are recoverable. The SDK cannot independently retry message posts. A timeout or uncertain response after posting begins is quarantined as `unknown`; a crashed `sending` claim is also held for operator reconciliation. Neither may be blindly resent. A new approved monthly update suppresses the old cycle and supplies the next due date.

Enable `MONTHLY_UPDATE_ROO_REMINDERS_ENABLED=true` in the scheduler environment only after deployment and authorized live acceptance with a connected founder. The normal dry-run command remains read-only and reports eligible startup groups without calling Slack or Customer.io. No schema change is introduced by chat reminders; the existing reminder ledger and Slack mirror schema must already be deployed.

## Customer.io setup

Create two transactional messages and paste in:

- `docs/customerio-monthly-update-reminder-7-day.html`
- `docs/customerio-monthly-update-reminder-1-day.html`

The MLAI Customer.io workspace currently uses transactional message ID `5` for the seven-day reminder and ID `6` for the one-day reminder.

Use the subjects and preheaders documented at the top of each file, retain the workspace preference/unsubscribe footer, and set their transactional message IDs in:

```env
CUSTOMERIO_MONTHLY_UPDATE_7D_TEMPLATE_ID=
CUSTOMERIO_MONTHLY_UPDATE_1D_TEMPLATE_ID=
```

Every API request explicitly sets `send_to_unsubscribed=false`. The call-to-action signs the user in, validates that they own the addressed company, switches Founder Tools to it when needed, and opens the create-update flow.

## Safe rollout on DigitalOcean

1. Deploy the backend and frontend changes through the normal reviewed release. Inspect existing schema requirements; applying any pending migration requires explicit approval for that migration.
2. Add both template IDs while leaving `MONTHLY_UPDATE_REMINDERS_ENABLED=false`.
3. Preview any local date without writes or email:

   ```sh
   docker compose exec web python manage.py run_monthly_update_reminders --date 2026-07-23
   ```

4. Send Customer.io test payloads from the comments in each template.
5. For the first genuine due batch, set `MONTHLY_UPDATE_REMINDERS_ENABLED=true` and keep `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=true`. Review and release the generated drafts in Customer.io.
6. After the draft batch is approved, set `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=false` for subsequent automatic delivery.

The normal send command is intentionally explicit:

```sh
docker compose exec web python manage.py run_monthly_update_reminders --date 2026-07-23 --send
```

The scheduler uses the same sending path once enabled.

## Email audit and duplicate protection

Each recipient, reminder kind, and local date has one `MonthlyUpdateReminderDelivery` row, visible read-only in Django admin. A successful request records the Customer.io delivery ID and response. A request that raises after dispatch begins is marked `unknown` and is not retried automatically, because Customer.io may have accepted it before the connection failed. Check Customer.io before taking any manual action on an `unknown` row.


## Local regression coverage

`startup_updates.tests_roo_update_reminders_unit` runs through `scripts/test_without_database.py` without a database or network. It covers ABN-only nonprofits, approval eligibility, exact 30-day expiry across daylight saving, next-cycle selection, company links, owner consent/revocation, bot identity, independent email/chat configuration, deduplication, bounded retries, concurrent claims and ambiguous delivery. The existing database-backed email suite is updated for approved 30-day windows; running it still requires the repository migration approval.
