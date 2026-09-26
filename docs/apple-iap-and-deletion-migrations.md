# Apple IAP and account-deletion migration proposal

Status: Sam explicitly approved this proposal on 21 September 2026, including
creation, disposable test/CI migration dependencies and production rollout after
checks pass. Both exact migrations have been created and applied in disposable SQLite and
PostgreSQL tests. Production has not been migrated as part of this change.
Requested by Sam on 21 September 2026 for App Store release.

## roo.0041_apple_iap_digital_credits

Depends on `roo.0040_merge_coworking_operations_office_manager` and the swappable
user model. Additive schema only; existing balances and purchase records are not
reclassified or changed.

- Add `digital_balance_microroo` and `digital_refund_debt_microroo` to
  `PointsAccount`, nonnegative big integers defaulting to zero. Apple credits
  are separate from the existing general/physical-reward balance. Reversed
  already-spent credits suspend further digital spending until reconciled;
  they do not become a monetary debt or silently consume earned points.
- Add `digital_delta_microroo` (signed bigint, default zero),
  `purchased_delta_microroo` (nullable signed bigint for allocation provenance),
  and `refund_of` (nullable self-reference, SET_NULL) to `Ledger`. Existing
  ledger records remain unchanged. These preserve the credit source through
  failed-service refunds and prevent conversion into physical rewards.
- Create `AppleIapTransaction`: UUID primary key; nullable user FK (SET_NULL);
  unique `(environment, transaction_id)`; original transaction ID; account-token
  UUID; product ID; quantity; granted microroo; verified price/currency; purchase,
  revocation and processing dates; status; latest signed-event timestamp;
  signed-payload SHA-256; nullable grant/reversal ledger references (SET_NULL).
  Index owner/environment/date and transaction status. Do not store card data,
  Apple IDs, private keys, or unbounded raw JWS payloads.
- Create `AppleIapNotification`: notification UUID primary key; verified type,
  subtype, environment and signed date; payload SHA-256; nullable transaction
  FK (SET_NULL); processing status, timestamps and bounded non-sensitive error
  code. Unique UUID plus transaction state serialize retries/out-of-order events.

Use the existing random `User.community_chat_profile_id` as Apple's
`appAccountToken`. It identifies the owner; it is never authentication. Verify
Apple's signature, app identity, product, environment and account binding before
any grant. Sandbox transactions cannot credit a normal production account.

## community_chat.0013_account_deletion_tasks

Depends on `community_chat.0012_member_onboarding`. Create a resumable
`AccountDeletionTask` table linked to the existing deletion request with CASCADE:
UUID primary key, unique `(request, target)`, bounded target name, status
(pending/processing/completed/needs_attention), attempts, next-attempt/started/
completed/updated timestamps, bounded error code, and a JSON evidence field for
non-sensitive counts and completion receipts. Index status/next-attempt.

No automatic account erasure, arbitrary retention exception, or data backfill
runs in this migration. Request acceptance and completion remain distinct.
Execution requires a member's confirmed deletion request. Completion is allowed
only after all applicable targets have verified cleanup; failures remain visible.
The chosen operational owner is MLAI committee via `hi@mlai.au`, with completion
within 30 days, as authorized by Sam's request for a standard owner/timeframe.

## Requested approval and rollout boundary

Approval requested for creating these two exact migrations, running them and
their already-existing dependencies in disposable SQLite/PostgreSQL test/CI
databases, and applying them in production as part of the requested rollout after
tests pass. Inspect `migrate --plan` first and stop for any additional unapproved
migration. No existing production user is deleted or balance changed by schema
installation. Do not reverse the schema after real IAP ledger writes; rollback
by disabling new sales while retaining transaction/refund processing.

Required validation covers invalid signatures/app IDs/products/account tokens,
duplicate and concurrent delivery, wrong environments, sandbox isolation, refund
and refund-reversal ordering, pending purchases across app restarts, digital vs
physical spending, failed-service credit allocation, and deletion retries with
partial cleanup. Financial ledger and credential data must stay out of logs.
