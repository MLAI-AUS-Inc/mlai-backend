# Roo Points purchase GST contract

The policy confirmed on 13 October 2026 treats purchased Roo Points as taxable sales with **10% Australian GST included in the advertised AUD pack price**. This contract implements that confirmed policy; it does not classify earned points, Apple purchases or other payment products.

| Opaque pack ID | Points | Total AUD | Included GST | Before GST |
|---|---:|---:|---:|---:|
| topup_5 | 10 | $19.99 | $1.82 | $18.17 |
| topup_10 | 20 | $36.99 | $3.36 | $33.63 |
| topup_25 | 50 | $63.99 | $5.82 | $58.17 |

## Checkout and evidence

`PointsPurchaseService` owns the policy for Slack-hosted buttons, the website and Community Chat's Stripe top-ups. Each new purchase records `metadata.tax_quote`; `/points/packs/` and the purchase response return an additive `tax` object with integer-cent gross, subtotal and GST amounts. Currency is AUD. The existing `amount_cents` field remains the gross amount payable.

New Checkout sessions require `ROO_POINTS_GST_TAX_RATE_ID`. Before creating the session, the backend retrieves that TaxRate using the existing Roo Points Stripe credential and verifies the exact ID, `active=true`, `inclusive=true`, `country=AU`, `percentage=10`, `tax_type=gst` and `display_name=GST`. Missing configuration, a rejected lookup or an incorrect rate prevents creation of an untaxed or tax-exclusive session.

The session sends the manual rate in `line_items[0][tax_rates][0]` and marks the inline price `tax_behavior=inclusive`. It preserves pack amounts, point quantities, terms consent, row locking and the existing purchase-scoped idempotency key. Session and PaymentIntent metadata record the GST policy version and cent amount. Successful session creation captures the tax snapshot in the existing purchase JSON metadata, requiring **no model or database migration**.

Stripe displays a manual rate on Checkout/receipts. This is separate from enabling automatic Stripe Tax or establishing tax registration. [Stripe manual tax rates](https://docs.stripe.com/payments/checkout/use-manual-tax-rates?payment-ui=stripe-hosted), [TaxRate receipt display](https://docs.stripe.com/api/tax_rates/object).

## Payment webhook

Both `checkout.session.completed` and `checkout.session.async_payment_succeeded` are supported. Points are credited only for a complete, paid session. For new GST sessions, the signed webhook must match the saved session, gross total, currency, included tax amount and policy version; the stored snapshot must be internally consistent. The backend records the provider's `total_details.amount_tax` as `metadata.tax_payment` before committing fulfillment. A new GST-marked session without its stored snapshot is rejected, rather than mistaken for a legacy session. Duplicate fulfillment remains idempotent.

`checkout.session.async_payment_failed` marks only a matching pending purchase failed and grants no points. A late failure cannot reverse a purchase already marked paid.

Previously paid purchases and existing session IDs are not rewritten. Reused pre-release sessions preserve their original Checkout configuration; no GST evidence is invented for them. They expire under the existing 24-hour policy. New pending purchases carry a quote and new successful sessions carry verified tax evidence. An old in-flight session that lacks a snapshot is not a proof of GST-free accounting: its Xero treatment remains governed by the confirmed purchase policy.

## Configuration and release

1. Identify or create an active, **inclusive** 10% AU GST TaxRate with display name GST and tax type gst in the **same Stripe account and mode** as the Roo Points payment key. The payment key must support TaxRate reads as well as its existing Checkout operation. Do not widen access without the required approval.
2. Set `ROO_POINTS_GST_TAX_RATE_ID` before releasing the backend. An empty value intentionally blocks new sessions.
3. Ensure the existing webhook destination subscribes to the two completed/succeeded events and the asynchronous failure event, retaining its signing secret.
4. Release the backend, then the website's GST labels and server-backed quote display. Validate a sandbox Checkout's total/tax and its signed webhook; no live charge is needed for development verification.
5. Preserve the already deployed Xero capture backoff hotfix when assembling a backend release. Keep Roo Points' existing reconciliation mapping on OUTPUT; manual GST snapshots do not authorise replay of previously posted payouts.

Production configuration/release is a separate authorised action. No new tax rate, permission, webhook subscription, deployment or payment is performed by the code change itself. Do not claim live checkout is verified until the configured rate and sandbox payment/webhook have been checked.

## Validation

Run the database-free suite with Python 3.11:

```sh
python scripts/test_without_database.py roo.tests_purchase_tax_unit
```

It forbids network and database connections and exercises the actual checkout/webhook functions with synthetic fixtures and mocked ORM/Stripe calls. It covers rounding, rate validation, missing configuration, failed HTTP calls, snapshot persistence, paid amount/tax mismatch, terms consent, retries, duplicate events, asynchronous outcomes and legacy sessions. It does not test database transaction concurrency.

`roo.tests_api.PointsPurchaseViewSetTests` is updated for the GST configuration and provider fixture. Running Django API suites or opening a backend PR invokes existing migrations in disposable databases, so obtain the specific migration approval required by [AGENTS](../AGENTS.md). No migration file is added by this change.
