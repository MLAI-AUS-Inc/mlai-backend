# Monthly update expiry reminders

The existing `scheduler` service invokes `run_scheduled_discovery` every minute. That command now also ticks the monthly-update reminder runner. The runner does nothing unless `MONTHLY_UPDATE_REMINDERS_ENABLED=true`, and it only selects reminders after the configured Melbourne send time.

## Eligibility and timing

A reminder is due only when all of these remain true at dispatch time:

- the user is active and has an email address;
- the user owns the registered, ACN-bearing, ABR-verified Founder Tools company;
- the user has a discount-eligible startup binding to the same organization; and
- the organization's latest ready monthly update is exactly seven days or one day from expiry.

The reminder window mirrors Roo pricing: the ready date plus 28 days is the final discounted booking date, and the rate expires the next day. There is deliberately no catch-up behavior, so enabling the feature cannot send old reminders.

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

1. Deploy the backend and frontend changes and run `python manage.py migrate`.
2. Add both template IDs while leaving `MONTHLY_UPDATE_REMINDERS_ENABLED=false`.
3. Preview any local date without writes or email:

   ```sh
   docker compose exec backend python manage.py run_monthly_update_reminders --date 2026-07-23
   ```

4. Send Customer.io test payloads from the comments in each template.
5. For the first genuine due batch, set `MONTHLY_UPDATE_REMINDERS_ENABLED=true` and keep `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=true`. Review and release the generated drafts in Customer.io.
6. After the draft batch is approved, set `MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=false` for subsequent automatic delivery.

The normal send command is intentionally explicit:

```sh
docker compose exec backend python manage.py run_monthly_update_reminders --date 2026-07-23 --send
```

The scheduler uses the same sending path once enabled.

## Audit and duplicate protection

Each recipient, reminder kind, and local date has one `MonthlyUpdateReminderDelivery` row, visible read-only in Django admin. A successful request records the Customer.io delivery ID and response. A request that raises after dispatch begins is marked `unknown` and is not retried automatically, because Customer.io may have accepted it before the connection failed. Check Customer.io before taking any manual action on an `unknown` row.
