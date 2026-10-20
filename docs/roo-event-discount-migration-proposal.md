# Roo event discount schema and disposable verification proposal

Proposed migration: `roo.0042_event_discount_redemptions`, depending on
`roo.0041_apple_iap_digital_credits` and the current swappable user dependency.
This proposal does not create or apply a migration.

The reviewable model source is `roo/event_discount_models.py`. A database-free
Django model check passed. A read-only migration preview identified exactly
three new tables, two indexes, and six constraints. No existing table columns,
balances, reward rows, or other data are changed by the proposed migration.

## Additive schema

- `EventDiscountOffer`: UUID; protected one-to-one reward catalogue reference;
  unique Luma event ID; ticket type ID; percent/amount discount fields; AUD
  currency; enabled flag defaulting false; start/end validity and timestamps.
- `EventDiscountQuote`: UUID; protected member and offer references; offer
  fingerprint; immutable JSON terms/event snapshot; private Slack workspace,
  channel and thread scope; quote expiry and creation time.
- `EventDiscountRedemption`: UUID; protected member, quote (one-to-one) and
  offer references; event ID; unique request ID and opaque coupon code;
  Luma coupon ID; processing/fulfilled/failed/cancelled status; general and
  purchased point allocation; protected one-to-one ledger reference;
  external-attempt timestamp; lease token/deadline; next attempt, attempt count,
  review flag and safe error code; fulfilment and creation/update timestamps.

Constraints enforce valid exclusive discount terms, AUD currency, valid offer
dates, one processing/fulfilled redemption per member/event, no terminal points
hold, and a ledger reference on fulfilled redemptions. Indexes cover quote
owner/expiry and redemption status/next-attempt work selection.

## Requested verification scope

After explicit user approval, create this migration file and run it and the
current required migration graph only against newly created, disposable local
SQLite and (if available) PostgreSQL test databases. Never use a production
database or credentials. Existing migration identities and SHA-256 hashes are
recorded in `output/event-discount-verification/existing-migration-inventory.json`:
366 existing migrations, before adding the proposed migration.

This approval does not cover deploying, migrating a persistent database,
enabling the feature, spending a real member's points, or creating a coupon on
a live event. Database-free tests continue independently while approval is
pending.
