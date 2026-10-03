# Unfinished Slack conversation recovery

A fresh Slack unread snapshot can coexist with an existing private mirror stuck
in `provisioning` with no MLAI channel. Fast recovery previously considered only
delivered activity and the older import catalog. Empty fields left a recently
active conversation waiting behind the full historical directory.

Authenticated source reads now advance eligible owner-inventory activity hints.
Advancing a hint wakes discovery only for an existing `provisioning` or `error`
mirror without a channel. The connection's lease, fairness position and provider
deferral remain untouched. Unchanged snapshots do not repeatedly schedule work.

Recovery can select that hint within the owner's history window, then still
checks current Slack membership, consent and device authority through the normal
idempotent registration ledger. It attempts one room per fair discovery turn.
A timestamp never grants access or creates a mirror by itself. General archive
admission retains its existing activity rules. Read positions are not reset.

## Review one exact stalled mirror

Use the existing production change-approval process. This dry run performs
bounded database reads and authority locks, without writes or Slack/relay calls:

```bash
python manage.py recover_slack_conversation_open \
  --grant-id GRANT_ID --device-id DEVICE_ID --source-id SOURCE_ID
```

Review the JSON's exact conversation, source, history window and plan fingerprint.
It excludes messages, profiles, credentials and device public keys. Missing,
paused, revoked, unconsented and already-provisioned mirrors fail closed.

Only after approval of the concrete action, supply the reviewed fingerprint:

```bash
python manage.py recover_slack_conversation_open \
  --grant-id GRANT_ID --device-id DEVICE_ID --source-id SOURCE_ID \
  --apply --expected-plan REVIEWED_PLAN
```

Apply rechecks authority under locks. A changed consent, device epoch, import
window, mirror or participant boundary invalidates the plan. It submits or
coalesces an intent in the existing bounded owner-open queue. The worker owns
source validation, retries, Retry-After, registration and publication. Queue
acceptance does not mean the chat is ready. Do not clear registrations, reset
read markers, force archive replay or create replacement channels as shortcuts.

Verify the existing mirror obtains a current active registration and channel,
then publication/presentation evidence and access for the selected device. Its
source read cursor must remain unchanged; the source-only unread should hand off
to the ready chat without duplication. A closed, inaccessible, source-limited or
out-of-window source retains the normal explicit failure.

## Validation

```bash
python scripts/test_without_database.py \
  community_chat.tests.test_slack_open_recovery \
  community_chat.tests.test_slack_open_requests
```

The runner forbids database/network access and applies no migrations. Coverage
includes dry-run fences, device revocation, queue routing, exact owner/source
query correlation, current source validation, backoff and checkpoint reuse.
Query compilation is not evidence of production recovery or load performance.
