# Shared startup update benefits

## Rule and compatibility

New discounts require an approved revision shared publicly or with the MLAI
community. A working draft's audience or generation status cannot qualify. The
shared receipt must match the revision audience and content hash. This applies
to coworking (8 → 4 Roo), article generation (12 → 6 Roo), boosted posts (8 → 4
Roo), and renewal reminder targeting through one shared benefit predicate.
Existing founder/director binding and verified Australian registration checks
continue to apply.

New private approvals earn 5 Roo points and no discount. The existing first
completion per startup per Melbourne calendar month can earn 20 only if shared
and registration/name/website verification succeeds; all other approvals earn 5.
A private approval still occupies the existing first-completion allowance.
Sharing a paid private update later does not award additional points. Reads,
edits, reapproval, duplicate requests, and another founder cannot pay it again.
Both direct and volunteer-ledger paths use the same amount. Existing ledgers
are retained verbatim.

The original first human approval anchors the fixed 30-day window. Sharing an
already-approved private update unlocks only the remaining window. Earned
discounts survive later working-copy edits or privacy changes until original
expiry; future bookings must also be inside that window. Historical recreation
ledger anchors, Melbourne booking dates and elapsed-time/DST boundaries remain
unchanged. No backfill of points or renewal of old discounts is performed.

## Compatibility migration

`startup_updates.0026_shared_update_benefits` depends on
`0025_sourceextractionreceipt`. It adds the Boolean field
`MonthlyUpdateApproval.requires_shared_update` with False for existing rows,
then alter its runtime default to True for new rows. Existing approvals are
therefore grandfathered without a calendar cutoff or guessed deployment time.
No existing approval content, audience, timestamp or points ledger is changed.
Legacy publications without canonical approvals retain their original anchor;
their first new receipt carries the old policy so editing cannot revoke it.

The disposable test scope includes the 370 unchanged existing migrations plus
this migration, recorded in the [371-migration inventory](shared-update-benefits-test-migrations-2026-10-21.json).
Its SHA-256 is `4adca95635725343471465882d2ecc09dc748323a6e1d2cec06779a9388b7f6b`.
Production schema application retains the existing exact-plan approval gate;
merging the code does not set that approval. The backend deployment leaves the
current runtime serving until the required migration has a matching approval.

## Copy and release dependencies

Companion Chat changes explain private/shared rewards in desktop audience
selection and review, registration setup, and mobile approval. Public/community
badges say “Up to 20”; the prior public “24” promise exceeded the backend tier.
Website setup guidance, Roo booking guidance/boost notices, renewal chat copy
and both checked-in Customer.io email templates carry the sharing condition.

The checked-in Customer.io templates are source artifacts. The external
Customer.io template bodies must be updated during an authorized rollout;
changing these files does not update the remote service. No message is sent by
this implementation task. Existing billing reservations and recorded booking
prices retain their historical receipts.

## Verification

199 database-free backend tests pass, including article pricing, exact
approval/disclosure receipts, first-approval clocks, expiry/advance booking/DST,
direct and volunteer reward credits, historical receipts, retries and ABR rules.
118 integration tests pass on a new disposable SQLite database using the
recorded 371-migration graph. They cover actual private/shared wallet credits,
coworking/article/boost prices, unchanged legacy expiry, reminders, revisions,
company scoping, replay protection, and migration upgrade defaults/preservation.
The disposable database is removed after the run; external connections and
credential loading are blocked.

Chat typecheck, five mocked browser approval journeys, 24 desktop tests,
audience-picker SSR, 11 mobile reward widget tests and mobile analysis pass.
The website frozen install, typecheck/internal links and 16 focused tests pass.
Three Roo notice tests, skill frontmatter validation and booking copy checks
with database/network access blocked pass. Hosted PR checks and merge records
provide final release validation; local results do not prove production rollout.
