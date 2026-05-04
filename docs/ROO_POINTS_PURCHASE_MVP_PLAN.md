# Roo Points Top-Up MVP Plan

This document describes the MVP for letting MLAI members purchase limited **Top-up Roo Points** through Roo and an MLAI frontend checkout page, while keeping Roo Points clearly positioned as community reward points, not money.

The updated implementation uses a local `PointsPurchase` row as the source of truth. Roo creates the pending purchase, the frontend loads it by purchase ID, Stripe Checkout processes payment, and the backend credits points only after a verified Stripe webhook confirms payment.

Current model decisions:

- `PointsPurchase.id` is a UUID primary key because the purchase ID appears in frontend URLs.
- Purchase requests expire using `expires_at`, not an `expired` status.
- The default request expiry is controlled by `POINTS_PURCHASE_EXPIRY_HOURS = 24`.
- Creating a Stripe Checkout Session does not change the purchase status; the purchase remains `pending` until payment succeeds, fails, is cancelled, or is refunded.

## Core Principle

Roo Points are not money.

Do not describe Roo Points as having a dollar value. Do not say:

- `1 Roo Point = A$X`
- Roo Points are worth a specific dollar amount.
- Roo Points are store credit.
- Roo Points are payment for work.
- Users earned a dollar amount by volunteering.

Use language like:

```text
Roo Points are MLAI's internal community reward points. They are not money, have no cash value, cannot be converted to cash, and cannot be sold or transferred. The price of Top-up Roo Points does not represent a monetary value for Roo Points.
```

## MVP Scope

### Included

- Slack-first top-up request handled by Roo.
- MLAI frontend checkout page keyed by `PointsPurchase.id`.
- Fixed prepaid Top-up Roo Points packs.
- Stripe Checkout for payment.
- Stripe webhook verification before points are credited.
- Separate tracking for earned points and purchased top-up points.
- Purchased top-up points are spendable on eligible MLAI rewards.
- Purchased top-up points do not count toward lifetime earned contribution, trust, leadership, committee eligibility, or paid-work consideration.
- Terms/privacy acceptance before Checkout.
- Conservative purchase limits.
- Confirmation posted back into the Slack thread where the purchase started.

### Not Included

- Cash-out.
- Refund to cash for unused points except where legally required.
- Peer-to-peer transfer.
- Selling or trading Roo Points.
- External marketplace.
- Crypto, blockchain, tokens, NFTs, wallets, or secondary markets.
- Points as payment for volunteers.
- Fixed public AUD-per-point calculator.
- Subscriptions or recurring billing.
- Custom card form in MLAI systems.

## Fixed Packs

Use backend-owned pack configuration for the MVP. Do not require Stripe Products or Prices yet.

| Pack | Price |
| --- | ---: |
| 5 Top-up Roo Points | A$19.99 |
| 10 Top-up Roo Points | A$36.99 |
| 25 Top-up Roo Points | A$63.99 |

Recommended backend constant:

```python
ROO_TOPUP_PACKS = {
    "topup_5": {
        "points": 5,
        "amount_cents": 1999,
        "currency": "aud",
        "label": "5 Top-up Roo Points",
    },
    "topup_10": {
        "points": 10,
        "amount_cents": 3699,
        "currency": "aud",
        "label": "10 Top-up Roo Points",
    },
    "topup_25": {
        "points": 25,
        "amount_cents": 6399,
        "currency": "aud",
        "label": "25 Top-up Roo Points",
    },
}
```

Do not display a price-per-point calculator. Stripe Products/Prices can be introduced later if non-engineers need to manage pricing in the Stripe Dashboard. For the MVP, backend configuration keeps pack policy in version control.

## High-Level Flow

```text
Slack user asks Roo to top up points
        |
        v
Roo checks linked MLAI account and parses/clarifies pack
        |
        v
Roo calls mlai-backend to create a pending PointsPurchase
        |
        v
mlai-backend returns purchase_id and MLAI frontend checkout URL
        |
        v
Roo replies in Slack with the MLAI checkout URL
        |
        v
Frontend loads purchase by purchase_id
        |
        v
User accepts Roo Points terms and privacy version
        |
        v
Frontend asks backend to create Stripe Checkout Session
        |
        v
User completes Stripe Checkout
        |
        v
Stripe sends checkout.session.completed webhook
        |
        v
mlai-backend finds PointsPurchase by Stripe metadata / session ID
        |
        v
mlai-backend appends ledger entry and credits purchased_topup points
        |
        v
mlai-backend or Roo posts paid confirmation in original Slack thread
```

## Backend Implementation

Backend repo: `mlai-backend`

### 1. Add Top-Up Point Accounting

Current `PointsAccount` fields:

- `balance`
- `lifetime_earned`
- `lifetime_spent`

Add fields:

- `earned_balance`
- `purchased_topup_balance`
- `lifetime_purchased_topup`
- `expired_or_reversed_points`

Behavior:

- Earned contribution points increase `balance`, `earned_balance`, and `lifetime_earned`.
- Top-up points increase `balance`, `purchased_topup_balance`, and `lifetime_purchased_topup`.
- Top-up points must not increase `lifetime_earned`.
- Spending points should continue to decrease spendable `balance`.
- If spend allocation is implemented in this MVP, spend oldest eligible points first and prefer expiring purchased top-up points before earned points.

Recommended ledger source types:

- `earned`
- `purchased_topup`
- `bonus`
- `admin_adjustment`
- `refund`
- `expiry`
- `redemption`

Recommended ledger transaction types:

- `credit`
- `debit`
- `adjustment`
- `reversal`

### 2. Add Purchase Model

Add a model called `PointsPurchase`.

Suggested fields:

```text
id: UUID primary key
user: FK to core.User
slack_user_id: string
pack_id: string
points_amount: int
amount_cents: int
currency: string, default "aud"
status: pending | paid | failed | cancelled | refunded
stripe_checkout_session_id: nullable unique string
stripe_payment_intent_id: nullable string
stripe_customer_id: nullable string
checkout_url: nullable URL/text
frontend_checkout_url: URL/text
terms_version_accepted: nullable string
terms_accepted_at: nullable datetime
privacy_version_accepted: nullable string
privacy_accepted_at: nullable datetime
purchase_from: JSON object
ledger_entry: nullable FK to Ledger
metadata: JSON
expires_at: datetime
created_at
updated_at
paid_at
```

Current MVP statuses:

```text
pending | paid | failed | cancelled | refunded
```

Do not add `checkout_created` for the MVP. After Stripe Checkout is created, keep the purchase as `pending` and use `stripe_checkout_session_id` / `checkout_url` to indicate that a Checkout Session exists.

Do not add `expired` for the MVP. Use `expires_at` to decide whether a pending purchase is still usable.

`purchase_from` stores origin-specific details. For the MVP, purchases originate from Slack through Roo:

```json
{
  "source": "slack",
  "slack_user_id": "U123",
  "slack_channel_id": "C123",
  "slack_thread_ts": "1712345678.000100"
}
```

This keeps the model flexible for future purchase origins such as the public `/roo` page, admin-created purchases, or other community surfaces.

Important constraints:

- `stripe_checkout_session_id` should be unique when present.
- `ledger_entry` should only be set once.
- `status='paid'` should be terminal for successful purchases except explicit refund/reversal workflows.
- Pending purchases must not be usable after `expires_at`.
- `expires_at` should default to `timezone.now() + timedelta(hours=POINTS_PURCHASE_EXPIRY_HOURS)`.

### 3. Add Purchase Limits

Enforce conservative launch limits before Checkout Session creation:

- Maximum 25 top-up points per purchase.
- Maximum 50 top-up points per member per rolling 12 months.
- Maximum 100 total spendable points balance unless manually approved.
- No top-up purchases for accounts less than 7 days old.
- No anonymous purchases.
- No guest checkout.

These limits should live in backend service logic, not Roo or frontend only.

### 4. Add Purchase Service

Add a service called `PointsPurchaseService`.

Create pending purchase:

```python
PointsPurchaseService.create_purchase(
    slack_user_id: str,
    pack_id: str | None = None,
    points_amount: int | None = None,
    purchase_from: dict | None = None,
) -> PointsPurchase
```

Responsibilities:

- Verify the Slack user is linked to an MLAI account.
- Validate or resolve the requested pack.
- Create a local `PointsPurchase(status='pending')`.
- Set `expires_at` using the backend expiry constant.
- Store Slack origin details in `purchase_from`.
- Return the `frontend_checkout_url`.
- Do not create a ledger entry.
- Do not create a Stripe Checkout Session yet if terms/privacy acceptance is collected on the frontend.

Create Stripe Checkout Session:

```python
PointsPurchaseService.create_checkout_session(
    purchase: PointsPurchase,
    terms_version_accepted: str,
    privacy_version_accepted: str,
) -> PointsPurchase
```

Responsibilities:

- Re-check purchase limits.
- Reject the request if `purchase.expires_at <= timezone.now()`.
- Store terms/privacy acceptance versions and timestamps.
- Create Stripe Checkout Session using dynamic `price_data`.
- Include local purchase identity in Stripe metadata.
- Save `stripe_checkout_session_id` and `checkout_url`.
- Keep `status='pending'` until a webhook confirms payment or the flow is explicitly cancelled/failed.

Stripe metadata should include:

```python
metadata={
    "points_purchase_id": str(purchase.id),
    "mlai_user_id": str(purchase.user_id),
    "slack_user_id": purchase.slack_user_id,
    "pack_id": purchase.pack_id,
    "points_amount": str(purchase.points_amount),
    "terms_version_accepted": purchase.terms_version_accepted,
    "privacy_version_accepted": purchase.privacy_version_accepted,
}
```

### 5. Add Purchased Top-Up Credit Service

Add a method separate from normal contribution awarding:

```python
PointsService.credit_purchased_topup(
    user: User,
    points_amount: int,
    purchase: PointsPurchase,
    stripe_checkout_session_id: str,
) -> tuple[Ledger, bool]
```

Responsibilities:

- Use a stable idempotency key:

```python
idempotency_key = f"points_purchase:{purchase.id}:paid"
```

- Create a `Ledger` row with:
  - positive points amount
  - `transaction_type='credit'`
  - `source_type='purchased_topup'`
  - `related_order_id=str(purchase.id)`
  - description that avoids assigning Roo Points a cash value
- Increase:
  - `PointsAccount.balance`
  - `PointsAccount.purchased_topup_balance`
  - `PointsAccount.lifetime_purchased_topup`
- Do not increase:
  - `PointsAccount.lifetime_earned`
  - `PointsAccount.earned_balance`

The ledger row is appended only after a verified successful Stripe payment event. Creating a pending purchase or Checkout Session must not create or reserve ledger entries.

### 6. Add Backend APIs

Create pending purchase from Roo:

```text
POST /api/v1/points/purchases/
```

Request:

```json
{
  "slack_user_id": "U123",
  "pack_id": "topup_10",
  "purchase_from": {
    "source": "slack",
    "slack_channel_id": "C123",
    "slack_thread_ts": "1712345678.000100"
  }
}
```

Response:

```json
{
  "id": "purchase-uuid",
  "status": "pending",
  "pack_id": "topup_10",
  "points_amount": 10,
  "amount_cents": 3699,
  "currency": "aud",
  "expires_at": "2026-05-06T00:00:00Z",
  "frontend_checkout_url": "https://mlai.au/roo/topup/purchase-uuid"
}
```

Read purchase for frontend:

```text
GET /api/v1/points/purchases/{id}/
```

Start Stripe Checkout from frontend:

```text
POST /api/v1/points/purchases/{id}/checkout/
```

Request:

```json
{
  "terms_version_accepted": "roo-points-terms-2026-05-04",
  "privacy_version_accepted": "privacy-2026-05-04"
}
```

Response:

```json
{
  "id": "purchase-uuid",
  "status": "pending",
  "stripe_checkout_session_id": "cs_test_...",
  "checkout_url": "https://checkout.stripe.com/...",
  "expires_at": "2026-05-06T00:00:00Z"
}
```

### 7. Add Stripe Webhook Endpoint

Add a webhook endpoint:

```text
POST /api/v1/points/stripe/webhook/
```

Responsibilities:

- Verify `Stripe-Signature` using `STRIPE_WEBHOOK_SECRET`.
- Handle `checkout.session.completed`.
- Read `points_purchase_id` from Checkout Session metadata.
- Fallback to `stripe_checkout_session_id` lookup if metadata is missing.
- Lock the purchase row with `select_for_update()`.
- Verify Checkout Session status and payment status are successful.
- Reject or ignore payment attempts for purchases that were already cancelled/refunded/failed.
- If already paid and ledger entry exists, return success without double-crediting.
- Credit purchased top-up points using `PointsService.credit_purchased_topup(...)`.
- Mark purchase as `paid`, set `paid_at`, save Stripe payment/customer IDs.
- Trigger Slack thread confirmation.

Optionally handle:

- `payment_intent.payment_failed`

### 8. Configure Stripe Webhook Delivery

Webhook setup is an explicit part of the MVP. It should be designed early, tested before launch, and configured manually in Stripe for the first release.

Recommended setup approach:

- Local development: use Stripe CLI webhook forwarding.
- Test/staging: manually create a test-mode webhook endpoint in the Stripe Dashboard.
- Production: manually create a live-mode webhook endpoint in the Stripe Dashboard after the production URL is confirmed.
- Automation: defer API-created webhook endpoints until after MVP unless there is a strong deployment need.

Webhook endpoint URL:

```text
https://api.mlai.au/api/v1/points/stripe/webhook/
```

Local development URL when using Stripe CLI forwarding:

```text
http://localhost:8000/api/v1/points/stripe/webhook/
```

Enabled events for Checkout MVP:

```text
checkout.session.completed
payment_intent.payment_failed
```

Recommended sequence:

1. Add the backend webhook view with signature verification.
2. Add tests that mock Stripe webhook construction and paid events.
3. Test locally with the Stripe CLI forwarding events to the local backend.
4. Manually configure a test-mode webhook endpoint in the Stripe Dashboard for staging/test deployment.
5. Copy that endpoint's signing secret into the test/staging environment as `STRIPE_WEBHOOK_SECRET`.
6. Verify a real test-mode purchase credits points exactly once.
7. Manually configure the live-mode webhook endpoint in the Stripe Dashboard.
8. Copy the live endpoint's signing secret into production as `STRIPE_WEBHOOK_SECRET`.
9. Run one controlled live-mode smoke test.

Environment variables needed:

```text
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
MLAI_FRONTEND_URL=https://mlai.au
```

### 9. Slack Confirmation After Payment

Store Slack routing fields on `PointsPurchase.purchase_from`:

- `source`
- `slack_channel_id`
- `slack_thread_ts`

When the webhook marks the purchase paid, post:

```text
Top-up complete. Your Roo Points have been added. These points can be used for eligible MLAI rewards, but they do not count toward lifetime earned contribution.
```

Preferred long-term option:

- Backend tells Roo via an internal endpoint/callback, and Roo posts the Slack message.

Fastest MVP option:

- Backend posts directly to Slack using the bot token if the backend already has the required Slack configuration.

Either option is acceptable for MVP as long as the purchase row stores the Slack channel and thread timestamp.

## Frontend Implementation

Frontend repo: `mlai-au`

### 1. Add Top-Up Checkout Page

Add a route such as:

```text
/roo/topup/:purchaseId
```

Responsibilities:

- Load `PointsPurchase` by ID from `mlai-backend`.
- Show the selected pack and price.
- If `status` is not `pending`, show the relevant paid/cancelled/failed/refunded state and do not start a new Checkout Session.
- If `expires_at` is in the past, show an expired-link message and ask the user to request a new top-up from Roo.
- Show required compliance copy.
- Show terms/privacy acceptance checkbox.
- Do not show an AUD-per-point value.
- Call backend to create a Stripe Checkout Session after acceptance.
- Redirect the user to Stripe Checkout using the returned `checkout_url`.

Required acceptance text:

```text
I understand that Roo Points are not money, have no cash value, are not refundable except where required by law, and cannot be transferred or sold.
```

### 2. Add Public Roo Page Copy

The public Roo page should explain:

- Roo Points are MLAI's internal community reward points.
- Roo Points are not money.
- Roo Points have no cash value.
- Roo Points cannot be converted to cash.
- Roo Points cannot be sold or transferred.
- Top-up prices do not represent a monetary value or exchange rate for Roo Points.
- Earned Roo Points count toward contribution status; Top-up Roo Points do not.
- Rewards are subject to availability and point requirements may change.
- Volunteering is voluntary and Roo Points are not wages or payment for work.

Suggested earned/top-up table:

| Type | How you get them | Can spend on rewards? | Counts toward contribution status? |
| --- | --- | --- | --- |
| Earned Roo Points | Volunteering, helping, contributing | Yes | Yes |
| Top-up Roo Points | Purchased through Roo | Yes | No |
| Bonus Points | Admin/sponsor/grant bonuses | Maybe | Usually no |

## Roo Implementation

Roo repo: `roo`

### 1. Update Skill Documentation

Update:

```text
roo-standalone/skills/mlai_points/SKILL.md
```

Add capability:

- Buy fixed packs of Top-up Roo Points.

Add action:

```text
topup_points
```

Add examples:

```text
/roo topup
top up Roo Points
buy Roo Points
add Roo Points
I need more points
```

### 2. Parse Top-Up Requests

Update Roo action resolution in:

```text
roo-standalone/roo/skills/executor.py
```

Recognition examples:

- `top up roo points`
- `buy 10 roo points`
- `add roo points`
- `I need more points`

If the user does not provide a valid pack, Roo should show the available packs without showing price per point.

Allowed packs:

```python
{"topup_5", "topup_10", "topup_25"}
```

### 3. Add Backend Client Method

Update:

```text
roo-standalone/roo/clients/mlai_backend.py
```

Add:

```python
async def create_points_purchase(
    self,
    slack_user_id: str,
    pack_id: str | None = None,
    points_amount: int | None = None,
    purchase_from: dict | None = None,
) -> dict:
    ...
```

This should `POST` to:

```text
/api/v1/points/purchases/
```

### 4. Handle `topup_points`

In `_handle_points_action(...)`, add a `topup_points` branch.

Behavior:

- Missing pack:

```text
Available Top-up Roo Points packs:
• 5 Top-up Roo Points - A$19.99
• 10 Top-up Roo Points - A$36.99
• 25 Top-up Roo Points - A$63.99

Top-up Roo Points are optional and do not count toward lifetime earned contribution.
```

- Unsupported pack:

```text
I can only help with these fixed top-up packs right now: 5, 10, or 25 Top-up Roo Points.
```

- Successful pending purchase:

```text
I created your Top-up Roo Points checkout. Continue here:
<frontend_checkout_url>

Top-up Roo Points are MLAI community reward points. They are not money, have no cash value, cannot be converted to cash, and cannot be sold or transferred.
```

## Webhook Matching Strategy

Use local database identity as the primary source of truth.

1. Backend creates `PointsPurchase` before creating the Stripe Checkout Session.
2. Frontend starts Checkout for that purchase ID.
3. Backend passes `purchase.id` in Stripe Checkout Session metadata.
4. Stripe webhook includes that metadata.
5. Backend loads the purchase row by ID.
6. Backend credits points using a ledger idempotency key.

Primary lookup:

```python
purchase_id = checkout_session.metadata["points_purchase_id"]
purchase = PointsPurchase.objects.select_for_update().get(id=purchase_id)
```

Fallback lookup:

```python
purchase = PointsPurchase.objects.select_for_update().get(
    stripe_checkout_session_id=checkout_session.id
)
```

This avoids guessing from amount, email, or Slack user ID.

## Idempotency Rules

Stripe may deliver webhooks more than once. Duplicate webhook delivery must not double-credit points.

Use all of the following:

- Unique `stripe_checkout_session_id` on `PointsPurchase`.
- Stable `Ledger.idempotency_key`.
- Transaction around purchase update and ledger credit.
- If purchase is already `paid` and has `ledger_entry`, return 200.

Example idempotency key:

```python
points_purchase:{purchase.id}:paid
```

## Test Plan

### Backend Tests

- Pack validation accepts only `topup_5`, `topup_10`, `topup_25`.
- Linked-account check rejects unlinked Slack users.
- Account age check rejects accounts less than 7 days old.
- Rolling 12-month cap rejects purchases above 50 top-up points.
- Balance cap rejects purchases that would push spendable balance above 100 unless manually approved.
- Pending purchase stores:
  - user
  - Slack user ID
  - pack ID
  - points amount
  - amount
  - `expires_at` about 24 hours after creation.
  - `purchase_from` Slack channel/thread.
- `PointsPurchase` status choices exclude `checkout_created` and `expired`.
- Checkout creation rejects purchases past `expires_at`.
- Checkout creation requires terms/privacy acceptance.
- Checkout creation includes purchase ID and acceptance versions in Stripe metadata.
- Webhook with valid signature marks purchase paid.
- Webhook credits `balance`, `purchased_topup_balance`, and `lifetime_purchased_topup`.
- Webhook does not increase `lifetime_earned`.
- Duplicate webhook does not double-credit.
- Existing earned points flow still increases `lifetime_earned`.
- Existing reward/coworking spend behavior still works.

### Frontend Tests

- Purchase page loads purchase by ID.
- Expired purchase page blocks Checkout and asks the user to request a new top-up.
- Compliance copy is visible.
- User cannot continue to Stripe without accepting terms.
- Continue action creates Checkout Session.
- Page redirects to returned Stripe Checkout URL.
- Page never displays a price-per-point value.

### Roo Tests

- `/roo topup` or `top up Roo Points` shows available packs.
- Unsupported pack is rejected.
- Valid pack calls backend with:
  - Slack user ID
  - pack ID
  - `purchase_from.source='slack'`
  - Slack channel ID and thread timestamp inside `purchase_from`.
- Successful response includes MLAI frontend checkout URL.
- Backend error gives a friendly failure message.

## Suggested Build Order

1. Add backend top-up accounting fields and migration.
2. Add backend `PointsPurchase` model and migration.
3. Add backend purchase limit validation.
4. Add backend pending purchase endpoint.
5. Add frontend checkout page keyed by purchase ID.
6. Add backend Checkout Session creation endpoint.
7. Add Roo command recognition and backend client method.
8. Add Roo `topup_points` response handling and tests.
9. Add Stripe webhook verification and idempotent paid handling.
10. Configure Stripe webhook delivery in test/staging.
11. Add Slack paid confirmation.
12. Add public Roo page copy updates.
13. Run backend, frontend, and Roo regression tests.

## Launch Checklist

- Roo Points Terms added.
- Privacy Policy updated.
- Public Roo page updated.
- Stripe Checkout tested.
- Stripe webhook idempotency tested.
- Purchased top-up points separated from earned points.
- Purchased top-up points excluded from lifetime earned contribution.
- Top-up caps enforced.
- No transfer/cash-out/resale functionality exists.
- Slack bot disclaimer shown before purchase.
- Frontend terms acceptance stored before Checkout.
- Admin audit path exists for every point movement.
- Refund/admin reversal process documented.
- Accountant has reviewed top-up/GST treatment.
- Lawyer has reviewed Roo Points Terms before launch.

## Open Decisions

### Should We Use Stripe Products/Prices?

Recommendation for MVP: no. Use dynamic Stripe Checkout `price_data` from backend-owned pack config.

Revisit if pricing should be managed by non-engineers in Stripe Dashboard.

### Who Posts The Paid Slack Confirmation?

Recommendation for MVP: choose the fastest reliable path.

Option A: backend posts directly to Slack.

- Faster if backend has Slack token/config.
- Slightly mixes payment and Slack behavior.

Option B: backend calls Roo and Roo posts to Slack.

- Cleaner service boundary.
- More moving parts.

Either way, store Slack channel/thread on `PointsPurchase.purchase_from`.

### Do We Implement Full Spend Allocation Now?

The brief asks for oldest-points-first redemption and purchased-point separation. The safest ledger design supports this now, even if the first implementation keeps redemption behavior mostly unchanged.

Minimum MVP invariant:

```text
Top-up Roo Points are spendable but do not count as earned contribution.
```

Preferred implementation:

- Track earned and purchased top-up balances separately.
- On redemption, allocate debits against eligible point lots.
- Record allocation metadata on the redemption ledger row.
