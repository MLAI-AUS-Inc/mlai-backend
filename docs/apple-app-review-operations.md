# MLAI Chat App Store operating decisions

Sam authorized Apple IAP, dedicated reviewer access and the contact details below
on 21 September 2026. This is a release handoff, not evidence that unfinished
features are deployed or that Apple has approved the app.

## Contact and deletion commitment

- App Review contact: Sam Donegan, `hi@mlai.au`, `+61401099433`.
- Account deletion owner: MLAI committee, reachable at `hi@mlai.au`.
- Completion commitment: within 30 calendar days of the confirmed request.
- The deadline starts at the original `AccountDeletionRequest.requested_at`.
  Retries, reassignment and partial cleanup must not reset it.
- The committee should acknowledge the receipt within two business days,
  triage outstanding requests daily and escalate anything still incomplete
  after 21 calendar days. These are operating targets, not claims of automated
  notifications already being sent.
- Preserve the request as pending/needs attention while any applicable cleanup
  fails. Never mark a request completed just because authentication was revoked.
- Completion confirmation must accurately describe any legally retained records
  and their limited use. Retention exceptions require a documented basis; they
  must not be invented by the cleanup code.

Production values, to set **after** cleanup is implemented and verified:

```dotenv
COMMUNITY_CHAT_DELETION_TIMEFRAME=within 30 days
COMMUNITY_CHAT_DELETION_CONTACT=MLAI committee: hi@mlai.au
```

The current deletion endpoint remains unconfigured because recording a request
is not yet a complete erasure workflow. The additive task-tracking migration
proposal is in [apple-iap-and-deletion-migrations.md](apple-iap-and-deletion-migrations.md).
It does not itself erase anything. Verification must cover credentials, profile
and onboarding answers, messages/media, bridge identity and copies under MLAI's
control, dependent application records, and eventual backup expiry. Do not erase
other members' or organisations' data to work around protected foreign keys.

## Dedicated reviewer account

A production account named App Review MLAI was created on 21 September 2026:

- Login identifier: `app-review@mlai.au`.
- No staff/superuser privileges, Slack connection, or pre-existing private chats.
- Password generated randomly, stored outside the repository in an owner-only
  operator file. Never copy it into this document, PRs, logs or fixtures.
- Normal onboarding remains required; no age, legal, marketing or AI consent
  has been manufactured on the reviewer's behalf.
- Native password sign-in is being added through the ordinary account endpoint.
  It must produce the same installation-bound account session and onboarding
  contract as email-code sign-in. Do not use a fixed OTP or reviewer bypass.
- Production password authentication is currently disabled. Enable it only after
  the new ordinary sign-in flow is tested and deployed.

Before submission, enter the dedicated credentials in Apple's App Review
Information, verify them using the release candidate on iPhone, and give Apple
clear onboarding/purchase/deletion test instructions. Do not claim reviewer
access is operational until that succeeds.

## Apple purchases

The app remains free. Proposed consumable packs match the existing price/quantity
schedule: 10 digital points for AUD 19.99, 20 for AUD 36.99, and 50 for AUD 63.99.
Use StoreKit's localized product price in the UI. Digital points do not expire
and must not pay for coworking, events or physical rewards.

The Paid Apps Agreement was `New` at inspection. Apple required a legal-entity
update before signing it; the account holder has been asked to complete that
business setup and any requested tax/banking steps. Draft product creation does
not activate sales or submit those products for review.

Release remains Australia-only, manual release after Apple approval, no France.
No App Store submission has occurred as part of this work.

## Approved implementation and validation (22 September 2026)

Sam approved `apple-iap-and-deletion-migrations.md` on 21 September. The exact
`roo.0041_apple_iap_digital_credits` and `community_chat.0013_account_deletion_tasks`
migrations now exist. Production inspection on 22 September showed no previously
pending migrations; these new migrations have not yet been deployed.

The backend now verifies Apple JWS using Apple's official server library, binds
transactions to the signed-in account, grants digital-only credit idempotently,
and processes ordered refund/reversal notifications. Content Factory and coding
spend digital credits first. Service failures restore the original digital,
purchased and earned allocation. Refunded consumed credit produces nonmonetary
digital debt, never a debit to physical-reward points. Generic identity merges
refuse accounts with Apple purchase lineage until purchase-aware reconciliation.

API contract:

- `GET /api/v1/community-chat/apple-iap/`: account token, catalogue IDs, balance
  and sales availability; normal approved Chat account session required.
- `POST /api/v1/community-chat/apple-iap/transactions/`: only `signed_transaction`
  JWS accepted. Credit/refund commits before ACK; ACK includes the exact product
  and transaction IDs. Session authority is rechecked after signature I/O.
- `POST /api/v1/community-chat/apple-iap/notifications/production/` and
  `/sandbox/`: independently verified Apple server notifications. No bearer
  credential replaces Apple's signature. Configure these URLs in App Store
  Connect before enabling purchases.
- `APPLE_IAP_ENABLED` defaults false and controls new sales. Previously purchased
  transactions continue to reconcile while sales are disabled.
- `APPLE_IAP_SANDBOX_ACCOUNT_TOKENS` is a comma-separated allowlist of synthetic
  account UUIDs. Those accounts accept only Sandbox receipts; ordinary accounts
  accept only Production. Sandbox lifetime grants are capped at 100 digital
  points per account. Refunds do not reset the test grant allowance.

Deletion requests now create one durable task per required storage boundary.
Task leases survive interruption, reject stale completion, preserve partial
failures, and keep the original 30-day deadline. The queue is read-only in admin.
This is tracking infrastructure: relay/media, external copies, shared account
cleanup and backup verification executors still need completion. Do not enable
production deletion or represent this as a complete erasure workflow yet.

Validation so far: 92 SQLite tests (two PostgreSQL-only skips), 64 PostgreSQL
purchase/coding/password tests including simultaneous delivery, 74 PostgreSQL
privacy/deletion-queue/content/refund tests, six signature-verifier unit tests,
and model drift checks passed. Further migration preservation checks and CI are
recorded in the pull request. These are synthetic tests, not Apple Sandbox or
physical-iPhone validation.
