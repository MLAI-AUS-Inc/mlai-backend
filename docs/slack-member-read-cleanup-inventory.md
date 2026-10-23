# Remaining bridge read conversion

Audit date: 23 October 2026. These are required follow-up changes, not a claim
that conversion has shipped. The permanent strategy PR removes selectors and
comparison writers; the read conversion must finish before cleanup is complete.

| Surface | Current legacy read | Required replacement |
| --- | --- | --- |
| `community_bridge/slack.py` and `worker.py` | Profile/mention/channel-name helpers use the bot | Current member authority before lower locks; prefer already-authorized payload metadata and stable ID fallback; never read a private mentioned channel to resolve its name |
| `community_bridge/community_delivery.py` | Bot write verification and uncertain-write reconciliation also read through the bot | Separate current member read authority from the immutable bot posting actor; lock the combined reader/author cohort before State/Channel/Delivery; retain bot writes and auth.test |
| `community_bridge/broadcast_mentions.py` | Legacy bot channel/user permission checks | Member-authorized source/mention metadata where the legacy write path remains; preserve explicit Roo/co-working behavior |
| Five historical repair/backfill commands | Thread history, avatars and mentions use bot helpers | Durable member-authorized repair scheduling; retire one-off commands that directly rewrite historical messages after the supported replacement is tested |
| `slack_workspace_users.py` | Background shared users.list with bot-token cache scope | Current authorized member directory source; credential/consent-bound cache and validation on cache hits |
| `slack_mention_directory.py`, `slack_mentions.py`, `slack_dm_directory.py` | Public bot reads and shared directory snapshots | Scoped member directory reads, keeping unlinked Community access and exact owner authority for private membership |
| `community_chat/slack_file_previews.py` | Public files.info/download fallback uses bot credentials | Current member grant with files scope, current channel/source permission, and existing file-to-message checks; keep private owner checks and cache isolation |

Audit all SDK construction and aliases as well as direct `SlackBridgeClient`
references. Keep `coworking.py`'s authorized bot write and `deletion.py`'s original
user-owned deletion path. Unrelated Roo or other application integrations are
outside the bridge read conversion.

Required tests cover member loss/recovery, consent/OAuth revocation between
selection and I/O, quota admission and lease expiry, private/Connect transitions,
cache hits after authority changes, uncertain writes without duplicate posts,
original bot actor preservation, and read-only repair planning. No new schema or
production data deletion is planned.
