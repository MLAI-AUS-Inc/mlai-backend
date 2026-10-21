# Slack member-sync evidence

Updated 21 October 2026. **The live behaviour spike has not run.** This file
separates Slack's documented contract from observations of the MLAI apps. It
does not certify Phase 0 or authorize an app-settings change or deployment.

## Owner decisions

- D1: retain existing consent. Do not introduce
  `slack-chat-v6-public-channels` or require a new acceptance. Existing grant
  consent, pause/revocation, scope, device and account checks still apply.
- D2: allow bot relay posts for accounts without a Slack identity link, provided
  the account is active, its device is verified and AI consent is current.
  Implement this only for the bot fallback in Phase 5.
- D3: use the MLAI workspace, in `volunteer` or `mlai-tech-team`, as instructed
  by the owner. This supersedes the proposed separate-workspace restriction.
  Channel discovery confirmed public `volunteer` and `tech_volunteers`, and
  private `mlai-tech-team`. Use `volunteer` for the public-channel cases.
  No test messages were sent.

Use an existing approved channel only for bounded test posts, edits, deletes
and reactions. Rename, archive and membership-removal checks require a dedicated
canary with staff accounts; do not apply those operations to a team channel.

## Documented contract and remaining observations

Sources below were checked on 21 October 2026. A documentation statement is
not a captured event from the configured MLAI user app.

| Question | Documented contract | MLAI observation still needed |
| --- | --- | --- |
| Public message visibility | Events are limited by the installation's scopes and visibility. Public messages use inner `channel_type: "channel"`. [Events API](https://docs.slack.dev/apis/events-api/), [message.channels](https://docs.slack.dev/reference/events/message.channels/). | Compare delivery when staff A and B are members, when only one is a member, and when neither is a member. Record user-app ID and authorization identities, without bodies. |
| Authorization expansion | An envelope can represent one of several visible installations. Expansion uses `event_context` and an app-level token carrying `authorizations:read`, rather than an ordinary user token. Some events lack context. [Authorization method](https://docs.slack.dev/reference/methods/apps.event.authorizations.list/). | Confirm the configured app can expand the staff test event, including pagination and authorization identity. |
| Join and leave events | Scope and context govern delivery. Membership-event `channel_type` can be a single-letter value; a `C` ID alone does not establish public visibility. [Joined](https://docs.slack.dev/reference/events/member_joined_channel/), [left](https://docs.slack.dev/reference/events/member_left_channel/). | Capture linked-self and other-user joins/leaves separately. Record actual shape and whether the linked user's own join/leave arrives. |
| Channel lifecycle | Slack documents [created](https://docs.slack.dev/reference/events/channel_created/), [rename](https://docs.slack.dev/reference/events/channel_rename/), [archive](https://docs.slack.dev/reference/events/channel_archive/), [unarchive](https://docs.slack.dev/reference/events/channel_unarchive/) and [deleted](https://docs.slack.dev/reference/events/channel_deleted/) events. Their existence does not prove receipt by this installation. | Verify supported user subscriptions and capture each event in a dedicated canary. Do not delete an existing team channel. |
| Channel ID changes | The current documented circumstance is conversion of a private channel's ID for Slack Connect. [ID changed](https://docs.slack.dev/reference/events/channel_id_changed/). | Do not create an external share to exercise this. Record unavailable as such; use a synthetic rekey test and retain Slack Connect exclusion. |
| External-share marker | The Events API defines envelope-level `is_ext_shared_channel`. Conversation objects also expose sharing state. [Events API](https://docs.slack.dev/apis/events-api/), [conversation object](https://docs.slack.dev/reference/objects/conversation-object/). | Inspect both envelope and event presence without persisting member content. Guards must reject either marker; absence is not evidence of membership. |
| Nonmember public info | `conversations.info` and the conversation object document token-relative `is_member`. [Info](https://docs.slack.dev/reference/methods/conversations.info/), [conversation object](https://docs.slack.dev/reference/objects/conversation-object/). | With staff A's official OAuth token, probe a public canary A has not joined and retain only success/error and `is_member`. |
| Nonmember public history | Slack documents user-token access to public history, including public channels the user has not joined. [History](https://docs.slack.dev/reference/methods/conversations.history/). | Confirm once in the canary. API readability must never grant a members-room audience or outbound posting authority. |
| Post echoes | User-token posting is documented, but the reference does not establish a universal echoed `client_msg_id` guarantee. [Posting](https://docs.slack.dev/reference/methods/chat.postMessage/). | Send a unique pre-stamped UUID with a synthetic test post through the actual MLAI user app and compare response and callback UUID presence. Observe create, edit, delete and reaction echoes. |
| Unfurl edits | `message_changed` is emitted for `chat.update`; automatic metadata changes can also produce it. [Changed message](https://docs.slack.dev/reference/events/message/message_changed/). | Use a benign link in a canary post; record whether changed callbacks arrive and whether text changed, without logging text. |
| Posting and history pacing | Slack applies workspace/app/method quotas and generally allows one posted message per second per channel. Other methods retain their documented limits. Restricted-distribution history has a separate slower quota. [Rate limits](https://docs.slack.dev/apis/web-api/rate-limits/), [history](https://docs.slack.dev/reference/methods/conversations.history/). | Confirm `MESSAGE_SYNC_SLACK_DISTRIBUTION` in the deployed configuration. Do not load-test the team workspace or intentionally provoke rate limits. |

## Corrections to carry into implementation

1. Classify payloads by event type. Applying the public-message
   `channel_type == "channel"` predicate to lifecycle and membership events
   would discard valid events. Resolve uncertain visibility using the authorized
   channel-state path, with membership and Slack Connect checks intact.
2. A successful public `conversations.info` or history response does not imply
   membership. Members-room access and sending require separate joined-state
   evidence, and revocations must shrink the audience first.
3. Do not treat an unknown or expired `event_context`, a partial authorization
   page or missing callback as proof that only one linked member could see it.
4. Existing-consent approval removes only the new-version prompt from the plan.
   It does not authorize paused/revoked grants or inventory outside existing
   consent boundaries.

## Live-spike evidence checklist

The connected Slack tools can discover channels and operate messages. They do
not expose callbacks, the MLAI app's authorizations, configurable `client_msg_id`
or a chosen member's official app token. Posting through that connector alone
would not test the migration's app identity or echo contract.

Run the spike from the approved app/server environment without copying tokens
or production member data into a local checkout. Before sending anything,
establish callback observability for the user app and identify two consenting
staff participants. Do not change Slack settings merely to complete this sheet.

For each case retain only:

- UTC time, case ID, app/workspace/channel IDs and synthetic staff aliases;
- event type/subtype, marker presence and authorization count;
- API outcome, `is_member`, UUID equality and whether text changed;
- test result and observer; never tokens, cookies, bodies or raw payload dumps.

| Gate | Status |
| --- | --- |
| Owner-approved workspace selection | MLAI workspace; bounded team-channel checks authorized |
| Staff A/B identified and linked through the user app | Pending |
| User-app callback/authorization observation available | Pending |
| Required subscriptions confirmed without changing settings | Pending |
| Message/echo/info/history cases executed | Pending |
| Dedicated lifecycle/membership canary identified | Pending |
| Observations reviewed and recorded | Pending |

Phase 0 remains incomplete until the live observations and the operational
checks in [the Phase 0 runbook](slack-member-phase0-operations.md) are recorded.
