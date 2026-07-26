# Humanitix to Xero reconciliation

Humanitix is imported as a payout source alongside Stripe. The integration is
payout-driven: Humanitix events, orders, and tickets provide attribution and
financial evidence, while the Humanitix global Payouts report provides the bank
deposit reference and amount.

## Safety rules

- Humanitix API keys are encrypted in `ExternalServiceConnection`.
- Buyer and attendee fields are never persisted. Only event catalogue fields
  and aggregate financial totals are stored.
- Stripe and `stripe-payments` Humanitix orders are context only. They remain
  accounted for through the Stripe payout ledger.
- Manual, cash, and invoice orders are never treated as Humanitix payouts.
- A Humanitix payout cannot become ready unless its Xero lines equal the payout
  amount to the cent.
- Import and preview do not write to Xero.
- Posting requires an explicit Points Admin request with `confirm: true`.
- Xero bank reconciliation remains a human action: posting creates an
  authorised Receive Money transaction, then the founder clicks Match/OK.

## Connect

From the authenticated founder UI:

```http
POST /api/v1/integrations/humanitix/connect
Content-Type: application/json

{
  "apiKey": "…",
  "companyId": 123
}
```

The key is validated against `GET /v1/events?page=1&pageSize=1` before it is
stored.

## Historical API sync

```bash
python manage.py sync_humanitix_history --domain mlai.au
```

Useful bounded checks:

```bash
python manage.py sync_humanitix_history --domain mlai.au --max-events 5
python manage.py sync_humanitix_history --domain mlai.au --incremental
```

The full sync pages every event, order, and ticket. Progress is committed per
event and recorded in the connection cursor, so reruns are idempotent.

## Payout report import and preview

Download the global **Payouts** CSV from Humanitix, then:

```bash
python manage.py import_humanitix_payouts /path/to/payouts.csv --domain mlai.au
```

Alternatively, a Points Admin can upload the CSV:

```http
POST /api/v1/integrations/reconciliation/humanitix/payouts/import
X-API-Key: …
Content-Type: multipart/form-data

slack_user_id=U…
domain=mlai.au
file=@payouts.csv
```

Inspect all records:

```http
GET /api/v1/integrations/reconciliation/humanitix/payouts
  ?slack_user_id=U…
  &domain=mlai.au
```

Refresh one preview:

```http
GET /api/v1/integrations/reconciliation/humanitix/payouts/{reference}/preview
  ?slack_user_id=U…
  &domain=mlai.au
```

Audit the full set against existing Xero bank transactions before posting:

```http
POST /api/v1/integrations/reconciliation/humanitix/payouts/correction-preview
X-API-Key: …
Content-Type: application/json

{
  "slack_user_id": "U…",
  "domain": "mlai.au",
  "max_count": 500
}
```

This endpoint is read-only against Xero. It distinguishes missing transactions
from already-correct entries, ambiguous matches, and legacy net-only entries.
Reconciled legacy entries must be unreconciled and replaced before the posting
endpoint will create anything, preventing duplicate Receive Money transactions.

## Explicit Xero posting

Only after the preview reports `ready: true`:

```http
POST /api/v1/integrations/reconciliation/humanitix/payouts/{reference}/post
X-API-Key: …
Content-Type: application/json

{
  "slack_user_id": "U…",
  "domain": "mlai.au",
  "confirm": true
}
```

Posting is idempotent by the Humanitix payout reference and checks Xero for an
existing BankTransaction with that reference before creating one.
