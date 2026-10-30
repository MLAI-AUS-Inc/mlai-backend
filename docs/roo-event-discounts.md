# Roo points for event discounts

Members ask Public Roo for an eligible event discount, review a ten-minute quote,
and confirm in the same private Slack-backed Roo thread. The backend reserves
general Roo points, creates one event/ticket-specific Luma coupon, records one
`EVENT` ledger spend, then returns the pre-applied booking link. The canonical
MLAI member account owns the receipt across devices.

## Configuration and supported offers

Community free-ticket offers cost **32 Roo Points**, provide **100% off one ticket**,
and require the ticket's live regular price to be **strictly below A$100**.
The provider boundary rejects A$100 and above both at quote and before issuance.
Use `configure_event_ticket_offer` to preview and save exact event/ticket terms;
see [community ticket pricing](roo-event-ticket-price.md). The generic EVENT_TICKET
redemption path is blocked because it cannot verify an event's live ticket price.

The feature is off by default. Backend configuration is:

```dotenv
ROO_EVENT_DISCOUNTS_ENABLED=false
ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID=
ROO_EVENT_DISCOUNT_WORKER_ENABLED=false
```

The existing `LUMA_API_KEY` must manage the event calendar and have Luma API
access. Existing isolated `ROO_API_KEY` and `INTERNAL_API_KEY` must be configured
and distinct; `MLAI_API_KEY`, if set, must also differ from Roo's credential.
Public Roo needs the matching workspace, feature flag and its own existing
service key configuration. Administrative Roo credentials are not a fallback.
The implemented assistant path is the existing verified Slack-backed Roo DM.
Chat's event CTA explicitly chooses that configured Slack target even if a
native Roo public key also exists. For general "Talk to Roo" requests during
the pilot, leave the native Roo public key unset unless its separate handoff
has been verified to supply the same trusted Slack actor context.

In Django admin, create a priced `RewardsCatalog` entry and an
`EventDiscountOffer` pointing to that reward, exact Luma event ID and exact paid
ticket type. Configure percent discount (1..100) or an amount in AUD cents,
expiry no later than event/ticket sales close, stock and activation. No live
offers or arbitrary exchange rate are seeded by the migration. Event offers
cannot use the generic reward redemption API; that path would not issue a
coupon and is rejected before spending or changing stock.

MVP eligibility is a public future MLAI-managed Luma event, registration open,
one ticket per registration, an available visible fixed-price AUD ticket with
no approval requirement, and an active configured offer. Capped ticket types
are checked using bounded approved-ticket evidence; that observation does not
reserve a seat. Date-only ticket sales windows fail closed because their exact
timezone boundary is not documented. Use explicit times or no sales window for
the pilot. Title discovery uses the existing public calendar feed (cached 60
seconds), filters to configured offers, and revalidates the selected event live.
Canonical event IDs avoid ambiguity; the Chat event card supplies that ID.

Only general earned/top-up points are eligible. Apple digital-only points stay
restricted to digital services. Coding reservations are projected into the
correct general/digital buckets; event holds are visible to all spending paths.

## Interfaces and authority

Strict isolated Roo-service endpoints under `/api/v1/points/event-discounts/`:

| Method/path | Purpose |
| --- | --- |
| `POST quote/` | Read configured offer, live event/ticket, eligible balance; save a member/context-bound quote |
| `POST redeem/` | Require exact true confirmation and a matching `Idempotency-Key`/`request_id`; reserve once and attempt fulfilment |
| `GET redemptions/` | Read this verified member's receipts |
| `GET redemptions/{uuid}/` | Read one member-owned receipt in its private scope |

Roo derives `slack_user_id`, `slack_team_id`, `slack_channel_id` and
`slack_thread_ts` from the verified original event, never model-selected actor
parameters. The backend requires the configured workspace, a private `D...`
channel, an active linked MLAI account and exact quote scope. Actor overrides
are rejected. Public messages must move to a DM before getting a quote. Roo
validates the original confirmation command deterministically; an LLM supplying
`confirmed` alone cannot authorize spending.

`GET /api/v1/community-chat/event-discounts/` uses the existing browser/native
account authentication and returns only that account's latest 50 receipts:

```json
{
  "enabled": true,
  "redemptions": [{
    "id": "redemption-uuid",
    "event_id": "evt-example",
    "event_name": "Example event",
    "status": "fulfilled",
    "points_cost": 10,
    "coupon_code": "ROO-ABCDEFGHIJKLMNOP",
    "checkout_url": "https://luma.com/example?coupon=ROO-ABCDEFGHIJKLMNOP",
    "expires_at": "2026-11-01T00:00:00Z",
    "created_at": "2026-10-20T00:00:00+00:00"
  }]
}
```

Statuses are `processing`, `fulfilled`, `failed`, `cancelled`. Only fulfilled
receipts disclose a code/link. All responses are private/no-store. Switching
off issuance retains account-scoped history. Home advertises only the boolean
capability flag; it contains no coupon history. Clients fence late responses
and old link-open callbacks against account/community changes and access denial.

Codes are opaque 20-character random identifiers, mapped internally to the
member and redemption. They do not reveal a UID. Luma enforces one code use,
not recipient identity; a member can share a link. Strict non-transferability
needs a different validated checkout mechanism. No seat, payment or ticket
purchase is made by this service. Coupons apply during initial registration.

## Durable recovery

A quote does not change points or stock. Confirmation locks the canonical
member and reward, rechecks saved terms/expiry/stock, creates one hold and
decrements the reward allocation. A unique constraint allows one processing or
fulfilled redemption per member/event. A unique request key and quote binding
make repeated confirmation the same operation.

The persisted random code precedes any Luma mutation. A worker lease fences
database state changes. Coupon creation is attempted once per receipt; after
an uncertain result, complete bounded coupon scans reconcile that saved code.
An absent or incomplete read never proves that a timed-out write did not happen
and never creates a different coupon. Exact remote terms and remaining use must
match before settlement. The final lock checks member status and expiry again.
One append-only ledger row records the exact purchased/earned allocation.

A definitive creation rejection releases the hold/restores stock without a
ledger charge. Uncertain results retain the hold and back off. Persistent
absence, changed/consumed coupons, expired coupons or an inactive member after
an external attempt require reviewed support; they are visible as
`needs_review` in read-only admin and aggregate worker status. A stale worker
never automatically disables an externally created coupon, which could
invalidate a newer worker's already fulfilled receipt. Coupon usage cannot be
inferred from `remaining_count:0` alone: Luma also uses zero for invalidation.

Unused-expiry/event-cancellation refunds and strict recipient binding are not
automatic in this MVP. Points are spent on successful coupon issuance. Resolve
ambiguous usage with Luma guest/order evidence before any manual refund or
invalidation. Generic account merging fails before mutation if either account
has event quotes/receipts; event-aware support must preserve that lineage.

## Migration approval and rollout

On 20 October 2026 the user approved creating
`roo.0042_event_discount_redemptions` and applying it plus recorded existing
prerequisites only to newly created disposable local test databases. See
[the exact proposal](roo-event-discount-migration-proposal.md). This is not
production migration, deployment, enablement, real points or live coupon approval.

The source adds an unconditional event-hold query to general points accounting.
Apply the specifically approved production schema before switching web/workers
to this source. The deployment's existing reviewed migration-plan gate remains
in force; a disabled issuance flag does not make an old schema compatible.

Release order, after separately authorized deployment/schema approval:

1. Apply the reviewed additive schema and release backend code with issuance off.
2. Set `ROO_EVENT_DISCOUNT_WORKER_ENABLED=true`; deployment starts the opt-in
   `event-discount-worker`, checks schema readiness without applying migrations,
   and supervises its successful-batch heartbeat. It participates in every
   writer-stop/rollback inventory. Issuance without the worker is rejected.
3. Release Public Roo with matching workspace/service contracts, then clients.
4. Configure a designated test event/offer and verify registration checkout,
   one-use behaviour, history and one points debit with explicit live-test approval.
5. Enable issuance for approved offers. Keep the worker enabled when disabling
   new issuance until all processing/review receipts have been resolved.

Safe operational status: `python manage.py reconcile_event_discounts --status`.
An explicitly authorized one-shot retry is
`python manage.py reconcile_event_discounts --limit 1`; the supervised profile
uses `run_periodic_command` every 30 seconds. Manual one-shot/loop commands never
create a new member request or change uncertain holds into refunds.

## Verification

Database-free provider/worker checks forbid database and network access:

```bash
.venv/bin/python scripts/test_without_database.py \
  tests.test_luma_event_discounts_unit tests.test_event_discount_worker \
  tests.test_periodic_command
```

The approved disposable runner clears inherited environment, disables .env
loading and blocks external networking:

```bash
.venv/bin/python scripts/test_event_discounts.py \
  roo.tests_event_discounts roo.tests_event_discount_api
```

PostgreSQL row-lock proofs run against a new local cluster in a dedicated
`/tmp/mlai-event-discounts-pg-*` Unix socket directory via the runner's
`--postgres-socket` option. No existing PostgreSQL service is reused. Tests cover
duplicate confirms, last allocation, competing event spends and competing workers.
CI also runs those suites in its existing disposable PostgreSQL lane.

Cross-repository tests cover natural-language routing, trusted actor/context,
literal confirmation, HTTP payloads, account-scoped history, malicious links,
late responses, mobile text scaling and actual desktop user interactions.
Verification output is recorded in `output/event-discount-verification/`.
