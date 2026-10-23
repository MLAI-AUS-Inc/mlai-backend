# Permanent Slack member strategy

This document specifies the permanent strategy. A merged source change does not
prove its production deployment; release evidence records exact running sources.

## Runtime contract

Community rooms retain their UUIDs, open audience and history. Members rooms use
current authorized Slack members and all verified devices within the 1,000-key
room limit. Existing consent is retained. Private, archived, excluded, held and
Slack Connect sources remain closed.

Every ordinary channel content read and callback requires current member read
authority. No-reader or uncertain membership means a recoverable hold with
checkpoints retained. A newly available reader resumes through the normal worker;
a changed consent/OAuth/membership scope restarts its cursor safely. Provider
cooldowns choose another eligible reader or delay work, never the bot.

Linked members post as themselves. Approved active, verified, AI-consenting
unlinked accounts retain bot posting. Edits, deletes and reactions preserve the
original actor. Bot posting also requires a current channel member for source checks,
mention validation and uncertain-write reconciliation. Losing that reader holds
the request; choosing another reader never changes the recorded bot posting actor.
Neither cleanup nor recovery replays an uncertain write or
changes its immutable request.

The former four rollout variables are no longer inputs. Keep production
`MESSAGE_SYNC_ENABLED=true`, `SLACK_OWNER_INVENTORY_ENABLED=true` and
`COMMUNITY_BRIDGE_PRODUCTION_ENABLED=true`. The optional
`SLACK_MEMBER_CHANNEL_ALLOWED_IDS` restriction remains supported. Retire old
repository/environment variables only after every last consumer is upgraded.

## Health and recovery

Run `python manage.py message_sync_status --check --local-worker` in the current
bridge worker, then inspect the normal content-free status report. Verify all
required lanes, current reader holds, retry cooldowns and backlog trends. The
Community report is read-only and covers current mappings, without a frozen
fifteen-channel comparison cohort or fabricated observation duration.

`MESSAGE_SYNC_ENABLED=false` is the existing incident processing pause, not a
rollout strategy. Apply an explicitly authorized pause coherently to all
consumers; preserve privacy reductions, queued work and receipts. Resume with the
same identities and current member authority. Routine cleanup deployment keeps
processing enabled. Prefer a compatible image rollback or forward fix that
retains enabled member behavior; an obsolete bot-mode variable cannot restore a
removed implementation.

Historical comparison receipt decoding stays until retained data no longer needs
it, including privacy purge. Do not delete queues, receipts, snapshots or room
registrations as code cleanup. No database migration is part of this change.

## Release evidence and remaining cleanup

Desktop 0.5.57 and internal TestFlight 1.0.0 build 97 contain the Members protocol
and delivery changes. Build 97 has not been submitted to public App Store review.
The owner passed desktop smoke checks and waived/deferred iOS/human checks; the
latter remain unperformed. A separate desktop release carries the subsequent
ctutils security patch. Server strategy removal changes no client protocol.

The owner requested expedited all-on cleanup on 23 October 2026. The earlier
seven-day comparison and fourteen-day stability gates were superseded, not passed.
Full relevant tests, exact-source release verification, registry preservation,
worker health and control continuity still supply the operational evidence.

Remaining read-helper/operator conversion and Slack bot subscription review are
tracked separately. Do not claim the whole cleanup complete until those changes
are deployed, the obsolete reconciler is absent, and the matching code/config
consumers have been removed. Keep independent Roo/co-working consumers and bot
posting credentials.
