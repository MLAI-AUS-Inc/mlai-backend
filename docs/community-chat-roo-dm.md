# Talk to Roo through the private Slack mirror

When no native Roo public key is configured, Community Home advertises the
first-party Public Roo Slack identity. The app explains what Roo can help with,
then opens the member's private Slack conversation with Roo using the existing
`POST /api/v1/community-chat/slack/dms/` contract. Members without an active grant
first connect Slack through the existing owner consent and OAuth flow.

This reuses Slack's one-to-one conversation ID and the owner's existing private
mirror. Opening it does not post a greeting or book anything. Messages the member
chooses to send are delivered using their own Slack authorization. Roo processes
them through its existing Slack DM handler, and its replies return through the
private mirror's live-event/history pipeline. No new agent creation endpoint,
native Roo identity or assistant deployment is required for this path.

## Configuration and boundary

- `COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID` defaults to `T05N9C1QSJC` (MLAI).
- `COMMUNITY_CHAT_ROO_SLACK_USER_ID` defaults to `U090FV0GTT4` (Public Roo).
- Either value can be blanked to disable the fallback. It is advertised only
  when `COMMUNITY_CHAT_RELAY_URL` has the canonical `chat.mlai.au` hostname.
- These public identifiers were verified against the connected MLAI Slack
  workspace and Roo bot profile on 8 September 2026. Names never select a bot.
- Opening a bot DM requires exactly one recipient and a fresh Slack `users.info`
  profile matching the configured workspace and active bot identity. The regular
  member directory remains human-only; other bots and bot group chats are rejected.
- Existing grant authorization, authenticated device binding, Slack conversation
  membership validation, private relay delivery, encryption, retries and
  owner-controlled disconnect apply unchanged.
- Bot messages remain filtered except for this exact Roo author in a Slack `D…`
  conversation. History additionally requires the exact owner/Roo participant set.
  Live event routing still checks each owner grant and source participant before
  enqueueing, and handles Roo edits/deletions through the same delivery ledger.

Connecting Slack does not make the member's other private conversations available
to Roo or to community analytics. This deliberate conversation with Roo is itself
an AI conversation; the app explains that messages sent there are shared with Roo
in Slack. The generic mirror privacy flag `included_in_roo=false` continues to
mean that imported conversations are not added to Roo's context or analytics.

## Rollout and verification

Ship backend and client changes together after the normal release approval. An
explicit deployment override takes precedence over the defaults above. Verify
that the existing Slack mirror worker, user scopes and Slack event subscriptions
are operational, then test with an authorized member: open Roo twice (same private
chat), send a harmless question, receive its reply, and see the same exchange in
Slack. This live exchange requires explicit testing authorization.

Local regression tests use synthetic members and mock Slack/relay transports.
They cover native/Slack selection, disconnected authorization, private destination
validation, verified bot selection, history membership, Roo live replies, edits
and deletions, and rejection of other bots/workspaces/group channels.
