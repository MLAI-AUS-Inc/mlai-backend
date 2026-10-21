# Article retry repair, 21 October 2026

A rejected article payment previously committed a workflow operation before the
debit. Each subsequent HTTP retry reused its client key but generated a different
analytics UUID, producing `operation_key_conflict` instead of the original payment
response. The production incident began with HTTP 402, no charge, no dispatch
outbox and no worker run. It was not a failure to resolve the merged user's account.

The workflow reservation now commits atomically with the debit and dispatch outbox.
Provider admission retains a pinned source/target snapshot outside the transaction;
concurrent source/target changes still reject billing. Failed debits roll back the
reservation. Analytics identity is stable for each organization/client key, and
previously charged retries recover original metadata after checking the original
requester. Linking Slack cannot replace an owned request's internal actor identity.
Changed article inputs and foreign requester identities still fail closed.

Validation: 148 targeted database-free tests passed. Two new PostgreSQL tests
exercise repeated payment rejection, rollback of the actual operation/outbox,
a top-up, a same-key replay after Slack linking, one charge only and changed-input
denial. Eighteen existing lifecycle/concurrency database regressions passed. Tests
used the already-approved 367-migration inventory; no schema change is required.

The incident's old rejected reservation can be released only after fresh authenticated
worker-key lookup proves absence and locked checks confirm the exact tenant,
connection, failed operation, no spend/refund, no outbox, no job and no run. Retain
the failed operation under a separate audit key with its original client identity,
private before-image and administrator attribution. Never reopen or rebind charged,
accepted, cancelled or otherwise ambiguous work.

Coordinate deployment with the Slack migration chat; integrate the alias and retry
commits in its preservation PR before the next main release. Scoped hotfix images
must layer on the actual currently running authentication images.
