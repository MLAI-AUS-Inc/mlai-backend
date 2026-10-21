# Slack member migration: Phase 0 operations

This is a baseline checklist for the member-token migration, recorded on
21 October 2026. It does not establish live database state or authorize a
deployment, migration, Slack app change, or production variable change.

## Recorded baseline

- The repository-level GitHub variable
  `COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED` was initially read as `true`. No variable
  with that name was found in the `production` GitHub environment. The
  workflow reads `${{ vars.COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED }}`;
  inspect both scopes again before the next release.
- [Backend deploy run 39392732962](https://github.com/MLAI-AUS-Inc/mlai-backend/actions/runs/39392732962),
  for commit `8ed420b2749ed3e92210fa581682e4cdf6672498` (PR #1175),
  passed its `checks`, `postgres-search`, and `migration-tests` jobs. Its
  deploy job failed. At 09:45:06 UTC on 20 October 2026 it reported:
  “Pending migration plan has no matching, specific approval; current runtime
  remains online.”
- That failure supports the report that #1175 did not replace the runtime.
  It does not prove that no other operation changed the database or relay.
  The count checks below remain outstanding.
- After explicit owner authorization, the repository variable was set to
  `false` and read back as `false` on 21 October 2026 at approximately
  01:38 UTC. No deployment or worker restart was performed by that change.
- No production database, Slack token, or private key was read for the initial
  GitHub baseline. Database and live-spike checks remain separately recorded.

Keep the lifecycle flag `false` before approving the next backend release,
as required by Phase 0. A repository variable value is release
configuration, not proof of the value currently loaded by a running worker.
Record the owner-confirmed runtime value separately.

A subsequent authorized read-only inspection found the running backend at
release `084ab7377898b7652d97a0909e5f1c23475c762f`, with integrations migrations
applied through `0048_owner_slack_conversation_inventory`. The lifecycle column
and state table introduced by 0049 are absent, and the lifecycle setting is not
defined by that runtime. Do not describe the absent-table checks as zero counts.
The runtime reports message sync and owner inventory enabled, and Slack
distribution `internal`. Recheck these observations after an approved release.

The Phase 1 capability header also needs this PR's additive browser and Tauri
CORS allowance deployed before the clients send it. The Tauri allowance is
restricted to the existing Slack account paths and trusted desktop origins.

## Owner checks after the next approved backend deployment

Run the following counts against the intended backend database using the
owner's established read-only connection. Do not copy credentials into this
document, a shell transcript, or a pull request. These queries need the schema
containing migration 0049. If a table or column is absent, report that result;
do not apply a migration to make the inspection pass.

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT COUNT(*) AS lifecycle_managed_channels
FROM community_bridge_channel
WHERE lifecycle_source <> '';

SELECT COUNT(*) AS lifecycle_workspaces
FROM community_bridge_lifecycle_state;
ROLLBACK;
```

Both counts must be zero. The private hold/exclude rows seeded by 0049 have
blank `lifecycle_source`; their existence alone does not show that the bot
lifecycle lane ran. Report an unexpected nonzero count to the owner before
continuing. Do not delete or reconcile rows during verification.

For a Django-level equivalent, the owner can evaluate only:

```python
CommunityBridgeChannel.objects.exclude(lifecycle_source="").count()
CommunityBridgeLifecycleState.objects.count()
```

On the relay database, inspect the intended community and configured bridge
public key. The following uses `psql` variables `community_id` and
`bridge_pubkey`, supplied through the owner's normal connection procedure.
The public key must be the bridge signing identity, not the relay owner.

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT COUNT(*) AS rooms_created_by_bridge
FROM channels
WHERE community_id = :'community_id'::uuid
  AND created_by = decode(lower(:'bridge_pubkey'), 'hex');
ROLLBACK;
```

The count must be zero before the new relay adoption/membership rollout. The
15 existing community rooms were created by the relay owner. Do not treat
later adoption of those rooms into a bridge registry as evidence that the bot
created them. Investigate any nonzero count before attributing it to a
particular deployment. Retain the count and explanation without copying room
names, member lists or messages.

## Interpreting the coverage report

From the intended environment, using its existing read-only database access:

```bash
python manage.py slack_member_coverage --workspace T123ABC456
```

Replace the synthetic workspace ID with the intended Slack workspace ID.
`--max-age-hours 1` tightens the default
24-hour evidence window. The command requires an explicit workspace and emits
JSON only. Retain the result in the restricted operational evidence store;
it contains account and Slack IDs and must not be pasted into public logs.

`slack_member_coverage` is an offline report over stored state. It must not
refresh credentials, call Slack, collect inventory, create rooms, or alter
consent. Its result is readiness evidence, not proof that Slack will accept
the next request. It defaults to a 24-hour maximum inventory observation age;
use `--max-age-hours` for a stricter operator-selected bound. Read both the
per-member inventory status and the report's `inventory_gaps` alongside each
channel's `observed_member_count`.

The report uses independent SELECTs, not one database snapshot. Its
`snapshot_consistency` field discloses that inventory and revocations can change
while it runs. Do not use a report as an access decision or an automatic source
switch gate. The canary and actual reader selection must revalidate current
grant, identity and channel membership fences at execution time.

Distinguish these conditions:

1. An active identity link is account-backed, not revoked, and matches the
   grant's MLAI account, Slack workspace and Slack user. The MLAI account must
   still be active. A legacy public-key-only link cannot authorize a member
   reader. Whether an account has an active verified device is a separate
   audience readiness check.
2. A locally eligible grant is active and not revoked. Its Slack connection
   belongs to the same account, is `connected` or `syncing`, identifies the
   same Slack workspace/user, and records `channels:read`, `channels:history`
   and `chat:write`. Scope presence alone is not a live token validation;
   the report must not load or decrypt access or refresh tokens.
3. Existing inventory metadata consent must match the current grant,
   connection, consent generation/version, OAuth generation and scopes. The
   owner's decision is to require no additional public-channel consent. That
   does not revive paused/revoked grants or older inventory consent epochs.
4. Public inventory is collected with `users.conversations`, which represents
   the owner's channel membership. A visible public channel obtained from
   `conversations.list` or a successful nonmember `conversations.info` call
   must never be counted as membership. The current inventory table has no
   `is_member` column; report the membership source and its limits explicitly.
5. A row is an observation, not a fresh membership check. Disclose incomplete
   sweeps, stale timestamps, missing current consent and unsupported/shared
   rows. Exclude unknown or stale evidence from confirmed reader counts.
   Never label an unknown channel as having zero live readers or as having
   two confirmed readers simply because two older rows exist.

Before the Phase 6 source switch, rerun the report and verify at least two
current, consenting readers for every community channel. Coverage alone does
not replace the seven-day comparison window or the canary.

## Completion evidence to retain

- Owner-confirmed lifecycle flag change and loaded runtime value.
- Approved deployment identifier and the count-only backend/relay results.
- Coverage report version, observation time, per-channel reader counts,
  missing-scope counts and explicit unknown/stale coverage.
- Bounded Slack behavior-test results with the chosen channel, participating
  staff accounts represented by synthetic labels, and no message bodies or
  credentials. See [the Slack facts record](slack-member-sync-facts.md).

Do not mark Phase 0 complete until these owner observations and the required
behavior-test results exist. Source inspection and passing fixture tests
cannot stand in for them.
