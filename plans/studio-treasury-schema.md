# Studio treasury schema proposal

Migration `integrations/0052_studio_meetings_treasury` adds three models.

- `StudioTreasuryLedger` has one row per organisation and a unique Slack
  workspace. Reviewed source configuration and opening evidence accompany the
  JSON reservation ledger. Every authorisation locks this row, serializing all
  builder and customer allocations in that organisation.
- `StudioPaymentReview` stores immutable source claims, a source fingerprint,
  requester, expiry, result and receipt. Its `(ledger, request_key)` constraint
  makes signed Slack requests replayable without a second authorisation.
- `StudioMeetingOccurrence` stores provider occurrence identity, project,
  calendar evidence, Linear issue and per-person attendance. Its
  `(ledger, occurrence_key)` constraint collapses recurring/calendar copies and
  requests from different Slack threads.

The migration creates empty tables and constraints. It contains no data rewrite,
deletion, backfill or production setup. Schema creation, disposable test
application and the database regression suite require the specific approval
requested from Sam. Production application uses the separate release process.
