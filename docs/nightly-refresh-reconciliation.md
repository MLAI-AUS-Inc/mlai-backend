# Nightly island refresh reconciliation

Nightly topic refresh uses `island_refresh`; ordinary topic discovery uses
`auto_discovery`. Paid brief-led island research also uses `island_refresh`, but
has a different admission and settlement owner. The nightly scheduler already
requires researched keyword rows and retains its per-organization/date dispatch
key. This change does not create work, backfill historical runs or retry paid
research.

`CONTENT_FACTORY_NIGHTLY_REFRESH_RECONCILIATION_SINCE` is an explicit aware ISO
creation boundary, disabled by default. Empty, `disabled`, invalid and naive
values admit nothing. A candidate must be created at or after the boundary,
queued/running, stale and outside the recent-probe window. It shares the existing
bounded reconciliation batch.

After coarse selection, the saved request must have integer
`island_refresh_recovery_version: 1`, exact run ID/domain/workflow,
`request_source: mlai_backend_scheduler`, and boolean `include_expansion`.
Only those fields plus optional `client_request_id` are accepted. Unknown
fields, even null website or paid-research fields, are refused before any probe.
The Run must retain its organization and have no repository. This intentionally requires review when a new
producer changes the request contract.

## Preview and release order

First deploy and verify CF #1208's newly versioned requests, saved-scope resume,
lease-aware orphan sweep and optional expansion receipt. Historical unmarked
refreshes are never enrolled. Before choosing an activation boundary, inspect a
bounded cohort with:

```sh
python manage.py preview_nightly_refresh_reconciliation --created-since 2026-11-02T12:00:00Z --limit 10
```

The timestamp is illustrative, not approved. This command returns aggregate
counts only, writes nothing and makes no provider calls. `candidate_rows` counts
the coarse database cohort; `inspected` is bounded by the limit (1–100), and
`selected`/`refused_scope` report strict request admission within that batch.
It does not predict a repair or run any other reconciliation subpass.

After reviewed preview and release approval, carry the exact timestamp through
the GitHub variable and normal deployment. Verify the scheduler's effective
value and observe new runs before expanding scope. Set the deployment value to
`disabled` for rollback; empty preserves the host value and is not a rollback.
No production value is enabled by this implementation.

## Status ownership and limits

The backend adopts CF status under the current Run lock, retaining its identity
and original request. Foreign identity is rejected, a changed saved scope is
refused, and a terminal callback or cancellation that wins the race is preserved.
A confirmed 404 fails only the still-admitted active nightly Run, without
entering article credit/refund handling. Unavailable CF responses remain unknown.

CF owns worker restarts. The backend never invokes article `/recover`, cancel or
refund controls for this cohort. Existing island bulk-sync receipts remain the
owner of dispatch completion; observing Run completion does not synthesize that
data-sync receipt. The scheduler's existing stale-dispatch behavior is unchanged.

The normal reconciliation command remains mutating, with a limit per subpass
rather than a global action cap. Its other workflow and settlement subpasses
are unchanged. This rollout does not transfer Job progress ownership, activate
Run reads, resolve historical Jobs, or establish a successful live canary.
