# Remaining bridge read conversion

Audit date: 23 October 2026. These are required follow-up changes, not a claim
that conversion has shipped. The permanent strategy PR removes selectors and
comparison writers; the read conversion must finish before cleanup is complete.

| Surface | Current legacy read | Required replacement |
| --- | --- | --- |
| `community_bridge/slack.py` and `worker.py` | Profile/mention/channel-name helpers use the bot | Current member authority before lower locks; prefer already-authorized payload metadata and stable ID fallback; never read a private mentioned channel to resolve its name |
| `community_bridge/community_delivery.py` | Member-read conversion implemented; production release still requires verification | Current member reader is separate from the immutable bot actor. Combined reader/author locks precede State/Channel/Delivery; no-reader holds. Only writes and auth.test use the bot. Mention validation caches are bound to current grant/consent/OAuth and scopes are checked on cache hits. |
| `community_bridge/broadcast_mentions.py` | Legacy bot channel/user permission checks | Member-authorized source/mention metadata where the legacy write path remains; preserve explicit Roo/co-working behavior |
| Five historical repair/backfill commands | Thread history, avatars and mentions use bot helpers | Durable member-authorized repair scheduling; retire one-off commands that directly rewrite historical messages after the supported replacement is tested |
| `slack_workspace_users.py` | Member conversion implemented; release verification pending | Complete sanitized snapshots use a current member token, exact credential/consent/OAuth cache scope and live validation on cache hits; failed readers can yield to another member |
| `slack_mention_directory.py`, `slack_mentions.py`, `slack_dm_directory.py` | Member directory conversion implemented; release verification pending | Community names remain available to unlinked accounts through an eligible member; Members membership requires the requester's current joined grant and acknowledged device audience. Private searches retain owner validation. Cached public membership is rechecked against Slack before disclosure |
| `community_chat/slack_file_previews.py`, `slack_file_authority.py` | Member file conversion implemented; release verification pending | Fresh member files.info share proof and source INFO precede every metadata/image response, including image cache hits. Community uses a joined reader; Members/private require the owner. Consent, OAuth, source and device authority stay held through serialization/download. Cache scope binds exact credentials; external hosts/redirects never receive tokens. |

Audit all SDK construction and aliases as well as direct `SlackBridgeClient`
references. Keep `coworking.py`'s authorized bot write and `deletion.py`'s original
user-owned deletion path. Unrelated Roo or other application integrations are
outside the bridge read conversion.

Required tests cover member loss/recovery, consent/OAuth revocation between
selection and I/O, quota admission and lease expiry, private/Connect transitions,
cache hits after authority changes, uncertain writes without duplicate posts,
original bot actor preservation, and read-only repair planning. No new schema or
production data deletion is planned.

File-preview policy follows Slack's [file object](https://docs.slack.dev/reference/objects/file-object/)
and [download guidance](https://docs.slack.dev/messaging/working-with-files/#downloading-files).
Top-level file workspace fields can be absent; exact member authority and the
current shared conversation establish scope, and explicit mismatches are denied.
Metadata is freshly revalidated because an hour-old share list cannot authorize
a cached image after unsharing. Image bytes retain the bounded cache, and
provider budget/rate delays remain retryable without trying another token.
Only active grants and live private mirrors authorize provider reads; paused
imports retain stored history but cannot initiate file reads.
