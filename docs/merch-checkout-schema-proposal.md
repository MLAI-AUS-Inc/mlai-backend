# Merch checkout schema proposal — 8 September 2026

Status: proposed; no migration has been created or applied.

## User flow

Selecting a merch reward opens its checkout review in MLAI Chat on iOS,
desktop and web. Show the configured AUD item price, available Roo Points,
a single Apply Roo Points control, the exact discount, and the remaining AUD
total. Point use is optional. When points cover the item, Confirm order skips
Stripe. Otherwise Continue to payment opens Stripe Checkout. Completion is
confirmed by the backend, never inferred from a browser redirect.

AUD prices and pickup/shipping terms must be supplied before enabling products.
Do not derive a cash price from points or use sample prices in production.
Proposed discount: use min(available whole points, item cost_points), with the
same fraction of the AUD price discounted, rounded down to cents; full listed
points always cover 100% of the item price. This policy awaits user confirmation.

## Proposed migration: roo.0037_reward_checkout

Dependency: roo.0036_sanitize_coworking_operation_receipts and the existing
swappable user model dependency. No historical backfill, wallet credits,
existing-price changes or data deletion.

Add to RewardsCatalog:
- cash_price_aud_cents: nullable positive integer; null disables money checkout.
- checkout_enabled: boolean, default false.
- fulfillment_details: text, blank by default, customer-facing pickup terms.

Create RewardCheckout:
- UUID primary key; user FK (PROTECT); reward FK (PROTECT).
- client_request_id UUID with unique(user, client_request_id).
- immutable item name, AUD price cents, points cost, applied points, discount
  cents, amount due cents and fulfillment-details snapshots.
- status: pending/payment_pending/paid/cancelled/expired/refunded.
- unique nullable Stripe checkout session ID, session URL, payment intent ID.
- spend and reversal ledger FKs (PROTECT, nullable); redemption OneToOne FK
  (PROTECT, nullable); created/updated/expiry timestamps.
- nonnegative amount constraints, discount <= item price, amount due = item
  price - discount, applied points <= points cost.

Implementation must reserve stock and debit selected points atomically at
checkout creation, restoring the original earned/purchased point buckets and
stock exactly once on confirmed cancellation/expiry. Payment creation/retry
uses an immutable idempotency key. An uncertain Stripe response remains pending
for reconciliation; never release points while a session may still be payable.
Only a signature-verified paid Stripe event (matching session/order/currency/
amount) finalizes a money order. A zero-total order finalizes atomically without
Stripe. Existing reward fulfillment remains the authority for delivery.

Shipping beyond configured pickup terms needs a separate agreed fee/address
contract; do not silently assume free shipping. Product prices/tax treatment
must use the existing business's approved customer-facing prices.

## Requested approval scope

Create this migration and run it with the already approved 334-migration
closure only in synthetic disposable local databases and PR CI, including
forward/reverse schema checks. No ordinary local or production migration,
deployment, live payment, real purchase, or inventory modification is included.
