# Stripe payout reconciliation

This integration turns each paid Stripe payout into a durable ledger, attributes
the money to Luma events or other Stripe sales, and prepares one matching Xero
Receive Money transaction. It never attempts to read or mark Xero bank statement
lines: Xero does not expose that capability through the Accounting API. After a
transaction is posted, a human still reviews the suggested match and clicks
Match/OK in Xero.

## Data flow

1. `payout.reconciliation_completed` (or the daily backfill) retrieves the paid
   payout and every balance transaction included in it.
2. Luma sales use the immutable `event_api_id` copied into Stripe metadata.
3. Other Stripe payments are joined to invoices with `/v1/invoice_payments`.
   Roo Points purchases use their Stripe metadata. Refunds follow their original
   PaymentIntent back to the same source.
4. The payout, complete source rows, warnings and source hash are upserted into
   `StripePayoutReconciliation`. Webhooks and backfills never post to Xero.
5. An admin configures mappings, previews the exact Xero payload, and explicitly
   approves posting. Posting is deduplicated by payout ID locally and by Xero
   `Reference` before creation.

## Monthly-update enrichment

The Valley monthly-update workflow runs `reconciliation_enrichment` immediately
after its Gmail, Slack, Linear, Luma and other source facts have been merged into
the canonical timeline.

- Luma is authoritative for **Event Name**. Stripe's immutable Luma event ID is
  resolved only against the organisation's refreshed Luma event catalogue.
- Linear is authoritative for **Project Name**. A Linear record whose normalized
  name exactly matches a Luma event is marked as an `event_mirror`; it is useful
  corroboration for the event but is not automatically assigned as a project.
  Linear-only records are presented as project candidates.
- Gmail and Slack can support a short reconciliation review note, but the note
  must carry at least one concrete source evidence pointer. They never alter
  payout membership, amounts, account codes, tax types or GST treatment.
- Valley writes proposals into `ReconciliationSuggestion`. It cannot approve a
  mapping or post to Xero. A Points Admin must approve a current proposal before
  Event Name, Project Name and the note are copied into the deterministic
  `ReconciliationMapping` used by the Xero preview.

Machine endpoints used by Valley:

- `GET /api/v1/integrations/reconciliation/enrichment-context` returns
  outstanding Stripe source rows, Luma events, Linear projects and existing
  approved mappings for a specific monthly-update run.
- `POST /api/v1/integrations/reconciliation/enrichment-context` validates all
  source IDs and stores evidence-backed proposals.

The human decision endpoint is
`POST /api/v1/integrations/reconciliation/suggestions/{id}/decision` with
`decision: approve` or `decision: reject`. Approval is rejected if the Stripe
source hash changed after the proposal was generated.

## First-time setup

Run migrations `integrations.0024_stripe_xero_reconciliation` and
`integrations.0025_reconciliation_context_suggestions`, then reconnect the
existing Xero app with `accounting.banktransactions` and `accounting.settings`.
The old read-only token is not sufficient.

Configure `PUT /api/v1/integrations/reconciliation/profile` with:

- the Xero connection and bank account ID;
- revenue, fee and refund account codes;
- an explicit Xero tax type for each class (the app does not infer GST);
- `Inclusive`, `Exclusive`, or `NoTax` line amount treatment;
- the existing Event Name and Project Name tracking category IDs/names; and
- a Project Name option for Stripe fees that cannot be tied to a sale.

Configure `PUT /api/v1/integrations/reconciliation/mappings` for every immutable
source ID. Each mapping must explicitly choose:

- `accounting_treatment: revenue` when this payout is the transaction that
  recognises income; or
- `accounting_treatment: clearing` when an invoice or another Xero transaction
  already recognised the income. Clearing mappings require their own clearing
  account code and tax type. This prevents Stripe invoices already entered in
  Xero from being counted as revenue twice.

The mapping selects the approved Xero Event Name/Project Name option. During the
explicit posting action, the service resolves the name against the configured
tracking category and creates the option when it is missing. Monthly enrichment
and suggestion approval never mutate Xero on their own.

## Operations

- `POST /api/v1/integrations/reconciliation/report` runs a 1–92 day backfill and
  saves the ledgers. `GET` retains the downloadable read-only report.
- `GET /api/v1/integrations/reconciliation/payouts` lists workflow states.
- `GET /api/v1/integrations/reconciliation/payouts/{po_id}/preview` returns all
  readiness errors and the exact Xero payload.
- `POST /api/v1/integrations/reconciliation/payouts/{po_id}/post` requires a
  Points Admin, the service API key, and JSON `{"confirm": true, ...}`.
- `python manage.py sync_stripe_payout_reconciliation --days 7 --domain mlai.au`
  is the safe scheduled/daily repair path. It does not post to Xero.

## Posting gates

A payout cannot be posted unless Stripe balance transaction nets equal the bank
deposit, Xero line cents equal the same deposit, every source/refund is mapped,
the accounting/tax configuration is complete, the Xero write scope is present,
missing tracking options can be resolved with `accounting.settings`, and no
existing local or Xero transaction already uses that payout ID.

The Stripe API version is pinned to `2026-02-25.clover`. If a malformed value is
accidentally placed in the environment, reconciliation uses the pinned version
instead of sending invalid text in the `Stripe-Version` header.
