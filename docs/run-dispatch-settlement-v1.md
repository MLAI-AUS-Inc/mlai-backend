# Original dispatch closure and settlement

This component is a guarded consumer, not an admission switch or an activation
claim. New R3 flags remain off. Existing unmarked executions retain legacy
behavior. There are no migrations or production changes.

An authenticated missing-key lookup after the existing grace period is only a
candidate. Before settlement, the consumer calls `POST /api/runs/withdraw-dispatch`
with exactly `schema_version`, `client_request_id`, `backend_run_admission_v1`
and `backend_run_execution_v1`. Only HTTP 200 with the exact `closed_no_effects`
receipt is accepted. CF creates this immutable tombstone under the same lock as
dispatch admission/enqueue; every delayed initializer for that original key
refuses it. Claimed, failed, dispatched, started or uncertain keys remain held.

The receipt has exactly those two echoes, `schema_version: 1`,
`status: "closed_no_effects"`, the original `client_request_id`, `closed_at` and
`receipt_sha256`. Its hash is SHA256 of compact sorted JSON of the entire
receipt excluding `receipt_sha256`, with `ensure_ascii=True` and `allow_nan=False`.
It is not a signature: trust comes from the configured authenticated CF HTTP
connection. The committed fixture contains actual paired CF-produced bytes.

HTTP happens outside every SQL transaction. A durable nonce protects concurrent
withdrawal calls. The response must still match the canonical Run, original
request digest, current retained ticket and original candidate nonce. Closures
are retained under protected `run_owner_reconciliation_v1.dispatch_withdrawals`.
Lost acknowledgements retry the same original key and exact echoes.

Settlement locks the current organization and all bounded charge-sharing Runs.
All actual work must be durably closed, with no draft, delivery, journal or
publication uncertainty. An actual research source may carry a separately
sealed article debit; its earlier research work does not authorize article
work or prove article delivery. The sealed current debit and actual child
lineage stay separate from the immutable initial research charge.

The existing article/topic controllers own financial arithmetic, bucket
restoration, entitlement policy and original-key idempotency. The internal
`original_dispatch_refund_authority` context admits only a protected closure
and actual original payer/ledger/key/domain. `refund_authority_matches` checks
that exact scope, current ticket and immutable admission. An owned public
no-start refund without that scope must hold. This requires the paired refund
controller cutover; the consumer checkpoint alone is not ready for activation.

After an actual full original refund or verified entitlement release, the
consumer writes only private settlement metadata and typed billing facts.
Original requests, Run status, source clocks, approval and execution history
cannot change. A mismatched controller result or unintended original mutation
rolls back its financial transaction. A historical spend remains readable after
refund, but its actual refund link forbids funding another execution.

The existing Ledger column bounds a paid idempotency key including its legacy
charge prefix. The financial alias regression uses a 160-character logical
key, while dispatch/admission tests separately cover 255-character logical keys.
No financial key truncation, alias or migration is introduced here.
