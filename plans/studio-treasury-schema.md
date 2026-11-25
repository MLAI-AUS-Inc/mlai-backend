# Studio treasury schema

Migration `integrations/0053_studio_meetings_treasury` adds three models after
`integrations/0052_managed_channel_sections`.

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

The original Studio migration was exercised on disposable SQLite and
PostgreSQL databases. Renumbering preserves its three table operations and
constraints. On 26 November 2026 Sam approved the renumbered migration for
disposable tests and production only if the pending production plan consists
of those three table creations. PostgreSQL regressions check that simultaneous
approvals for two builders cannot reserve the same final customer hour.
