# Topic discovery reconciliation

The scheduler already reconciles admitted article and setup runs. Topic discovery joins that status polling only when `CONTENT_FACTORY_DISCOVERY_RECONCILIATION_SINCE` contains an aware ISO timestamp. Blank, `disabled`, invalid and naive values admit no discovery runs. Existing workflow behavior is preserved.

The boundary applies to `created_at`, including the exact boundary instant. Candidates must also be queued/running, stale and outside the recent-probe window. They share the existing bounded batch with other workflows; awaiting confirmation and other owner-action states are excluded. Do not backdate the boundary to replay historical stalled runs.

## Preview and activation

Before activation, deploy and verify Content Factory's saved discovery-scope resume fix (CF #1206). Its orphan recovery must retain the original island, custom topic, requested count and daily policy. CF's durable status endpoint supports discovery; its `/recover` endpoint currently supports article orchestration only.

Choose the approved activation time and preview selection:

```sh
python manage.py preview_discovery_reconciliation --created-since 2026-11-02T12:00:00Z --limit 10
```

This command only selects and counts rows. It makes no provider calls, invokes no reconciliation subpasses, sends no notifications and writes nothing. The limit is 1–100. `eligible` counts the cohort before the limit; `selected` describes one bounded batch. These are candidates, not predicted repairs or a successful canary. The example timestamp is illustrative, not an approved production boundary.

After a reviewed preview and release approval, carry the exact timestamp through the corresponding GitHub repository variable and normal deployment. The deployment validator, environment updater and Django setting all carry the same key. Verify its effective value in the scheduler container and observe newly admitted runs before expanding any rollout. Creating this configuration support does not activate it.

The normal `reconcile_content_factory_runs` command remains mutating and invokes additional repair and settlement subpasses. It is not a preview; its limit is not a global action cap.

## Ownership and rollback

The backend adopts returned status while preserving the current Run's identity and original request. Foreign identity is rejected, and a cancellation or completed callback that wins the race is preserved. A confirmed 404 uses the existing guarded missing-run failure/settlement path. Discovery worker restarts remain owned by CF's lease-aware orphan sweeper; the backend does not use its article-only recovery/cancel ladder for discovery.

Set the deployment value to `disabled` to stop admitting discovery candidates on later ticks. An empty release variable preserves the host's value, so empty is not a deployment rollback. Previously recorded settlement obligations remain with their existing owners; disabling candidate admission does not erase them. No historical Job creation, deletion, new schema or Run-read cutover is part of this rollout.
