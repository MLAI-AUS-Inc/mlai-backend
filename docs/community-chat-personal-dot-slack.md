# Talk to your own OpenAI dot through the private Slack mirror

An owner can talk to their own OpenAI dot (for example Maek) from MLAI Chat.
OpenAI connects a dot to Slack as a bot user of its ChatGPT Slack app. MLAI Chat
already sends an owner's private Slack messages with the owner's own Slack
authorization, so a conversation with the dot in MLAI Chat is the owner talking
to their dot in Slack. The dot keeps its own memory, plugins and approvals,
which stay with OpenAI.

This follows the Roo DM path (`docs/community-chat-roo-dm.md`). It does not use
the Roo DM actor handoff: the dot never calls MLAI.

## Switches

| Setting | Default | Meaning |
|---|---|---|
| `COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED` | `false` | Turns the feature on. |
| `COMMUNITY_CHAT_DOT_SLACK_APP_IDS` | empty | Comma-separated Slack app IDs (`A…`) whose bot users can be registered as a dot. Empty keeps the feature off. |

Both are GitHub repository variables plumbed through `deploy.yml`, `deploy.sh`
and `scripts/upsert_env_value_from_stdin.sh`. Set the app ID from a real dot
message in Slack (`app_id` on the message, or `profile.api_app_id` from
`users.info` on the dot's bot user).

## Registration

`GET`, `PUT` and `DELETE /api/v1/community-chat/slack/personal-dot/`, for the
signed-in owner with an active Slack mirror grant.

- `PUT {"slack_user_id": "U…"}` calls `users.info` with the owner's own Slack
  authorization. The user must be a bot in the owner's workspace, not deleted,
  not a stranger, not Roo, and its `profile.api_app_id` must be listed in
  `COMMUNITY_CHAT_DOT_SLACK_APP_IDS`. A display name never selects a bot.
- The registration is stored in the owner's Slack connection
  `provider_metadata["personal_dot_v1"]` with the workspace, the owner's Slack
  user, the dot's Slack user and app IDs, and its display name and picture.
  No migration. It survives a Slack re-authorization and is removed when Slack
  is disconnected.
- `DELETE` removes it. `GET` returns `{"enabled", "slack_connected", "dot"}`.

## What is accepted

- Opening the conversation uses the existing `POST /slack/dms/` with exactly
  one Slack user: the owner's registered dot, re-checked with a fresh
  `users.info`. Every other bot, and every group chat with a bot, stays
  rejected.
- Live events: a message, edit or deletion authored by a registered dot in a
  `D…` conversation passes the bot filter, then is routed only to the live
  mirror conversation of the owner who registered that dot and whose
  participants are exactly the owner and the dot. Anything else is ignored. Dot
  events are never staged for discovery, because staged routing cannot prove
  which owner registered the dot. History import recovers missed replies.
- History import accepts the dot's messages under the same owner and
  participant rules.

Not yet covered: the dot in private channels and channel mentions (plan Phase
2), and hiding the connector-based dot when this route is in use (Phase 3).

## Turning it on

1. The owner adds their dot to the MLAI Slack workspace from its ChatGPT
   profile and checks it answers a message MLAI Chat sends (plan Phase 0).
2. Set `COMMUNITY_CHAT_DOT_SLACK_APP_IDS` to the ChatGPT Slack app ID and
   `COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED=true`, then deploy.
3. The owner registers the dot in MLAI Chat and opens the conversation.
