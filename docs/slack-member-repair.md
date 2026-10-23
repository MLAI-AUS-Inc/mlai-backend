# Repair through the member sync worker

`message_sync_repair` replaces the six historical bridge replay/backfill commands.
It never reads Slack, publishes a relay event, resets a checkpoint or rewrites a
message link. It plans or schedules bounded work for the normal member worker,
which rechecks current consent, OAuth scopes, membership, source privacy and
device audiences before every page. No eligible reader means retained work and
a recoverable hold, with no bot-read fallback.

Plan one exact existing room (no writes):

```sh
python manage.py message_sync_repair --workspace-id TWORKSPACE --channel-id CCHANNEL
python manage.py message_sync_repair --workspace-id TWORKSPACE --channel-id CCHANNEL --thread-ts 1790000000.000001
```

After reviewing the content-free report, add `--apply` to schedule that selection.
Up to twenty exact thread roots may be supplied. There is no bulk workspace scan
or arbitrary historical replay mode. Public history retains its normal 30-day
window; an older thread root can locate replies within that window without
importing the old root body.

Existing head/archive jobs are preserved. An absent job is created once. A
completed head/thread scan can have its normal next pass brought forward only
when it has no checkpoint, active lease, backoff or error. Archive demand is
never reset or expanded. Provider budgets still apply in the worker. A paused
or revoked state, disabled mapping, HOLD/EXCLUDE decision, private source or
processing pause cannot be reopened by this command.

Use `message_sync_status` for progress and reader/budget diagnostics. Inspect a
specific retained message with `inspect_community_bridge`; do not delete its
receipt to force a resend. Normal source observations can correct content,
references and supported reactions. Missing history alone never authorizes a
delete. Historical cosmetic rewrites and delete/recreate orphan phases are
retired; this command does not promise those destructive replay operations.

The old `reconcile_recent_community_bridge_slack` entry point temporarily accepts
the previous container arguments but only seeds normal durable work when sync
is enabled. Its legacy scan is removed. Remove this compatibility entry point
with the retired service definition after production retirement is verified.

Release gate: this removal requires the tested replacement, full hosted CI and
a coordinated backend/relay/adapter backup with isolated restore evidence.
Keep the current member behavior enabled. Retain immutable commands, receipts,
poster identities, historical compatibility decoding and all existing room IDs.
Use a compatible image rollback or forward fix; do not restore an old bot/OFF
configuration or replace live data for a code-only rollback.
