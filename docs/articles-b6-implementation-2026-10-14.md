# B6 event wake implementation evidence

Current verified events wake through a bounded immediate executor and one
coalesced durable operation per connection/generation. Event hints contain only
immutable IDs, selected source/branch or owned PR identity. They never establish
approval, merge, CI or live proof. A running prepare claim retains its lease;
the durable wake stays pending instead of starting a concurrent pass. The normal
scheduler remains the fallback when the fast path/process is unavailable.

Source-only revision transitions have Django-owned completed ledger records.
Only a complete same-identity revision chain can rebind an active goal; proof is
invalidated and goal identity, start time, counters and budget are retained.
Terminal history is retained. Historical child references preserve metering when
retries/source changes replace their active pointers. Callback/CI and lifecycle
paths are wired, with every new flag off by default.

Eight focused event/scope/lease/coalescing tests passed; 104 goal and incident
neighbours passed. All 19 database-free checks-job lanes and model drift passed. The final
malformed-event guard is also covered by the focused rerun. No local database, migration or live webhook was used.
Production deployment remains a separate owner action. B2 dependencies remain
unmerged: the branch is explicitly stacked, not a release.
