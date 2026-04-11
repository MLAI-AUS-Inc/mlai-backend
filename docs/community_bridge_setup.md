# Community Bridge Setup

This repo already contains the Slack to Discord bridge runtime. The remaining setup is operator work:

1. Create a dedicated Slack app for the bridge.
2. Create a dedicated Discord bot for the bridge.
3. Set the bridge env vars in the backend deployment.
4. Register the Slack channel to Discord channel mapping.
5. Deploy or restart `web`, `scheduler`, and `bridge-worker`.

## Discord target from the URL you sent

The Discord URL you provided resolves to:

- Guild ID: `1492063515987410957`
- Channel ID: `1492063517191180340`
- Channel label in the screenshot: `welcome-and-rules`

For the pilot, a normal discussion or announcement channel is usually a better bridge target than `welcome-and-rules`. If you want the bridge in `#general` or `#announcements` instead, use that channel's URL when you create the mapping.

## 1. Create the Discord bot

In the Discord Developer Portal:

1. Create a new application for the bridge.
2. Add a bot user.
3. Enable `MESSAGE CONTENT INTENT` for the pilot.
4. Copy the bot token.
5. Copy the application ID.
6. Generate an invite URL for the bot and add it to the MLAI server.

Recommended permissions for the pilot:

- View Channels
- Send Messages
- Read Message History
- Manage Messages

## 2. Create the Slack bridge app

In Slack API app management:

1. Create a new app for the bridge in the target workspace.
2. Add bot token scopes:
   - `chat:write`
   - `channels:history`
3. Turn on Event Subscriptions.
4. Set the request URL to:

```text
https://<your-mlai-backend-host>/api/v1/integrations/bridge/slack/events
```

5. Subscribe to the bot event `message.channels`.
6. Install the app to the workspace.
7. Invite the app into the target public channel.
8. Copy the bot token and signing secret.

You also need the Slack bot user ID for loop prevention. One reliable way to get it is:

```bash
curl -sS https://slack.com/api/auth.test \
  -H "Authorization: Bearer $SLACK_BRIDGE_BOT_TOKEN"
```

Read the `user_id` field from that response and store it as `SLACK_BRIDGE_BOT_USER_ID`.

To get the Slack channel ID, copy the Slack channel link. The last path segment is the ID, for example `.../archives/C0123456789`.

## 3. Set backend environment variables

Set these in the deployment that runs `mlai-backend`:

```bash
SLACK_BRIDGE_BOT_TOKEN=xoxb-...
SLACK_BRIDGE_SIGNING_SECRET=...
SLACK_BRIDGE_BOT_USER_ID=U...
DISCORD_BRIDGE_BOT_TOKEN=...
DISCORD_BRIDGE_APPLICATION_ID=...
DISCORD_BRIDGE_PUBLIC_KEY=
```

`DISCORD_BRIDGE_PUBLIC_KEY` is optional for v1 because the current bridge uses the Discord Gateway, not Discord interactions.

## 4. Register the pilot channel mapping

After migrations are applied, create or update the bridge mapping with the management command:

```bash
python manage.py upsert_community_bridge_channel \
  --slack-channel-id C0123456789 \
  --slack-channel-name community-pilot \
  --discord-url https://discord.com/channels/1492063515987410957/1492063517191180340 \
  --discord-channel-name welcome-and-rules
```

If you want to point the pilot at a different Discord channel, replace the `--discord-url` value with that channel's link.

The command is idempotent. Running it again with the same Slack channel ID updates the existing mapping.

## 5. Deploy and verify

The runtime services are already wired in this repo:

- Slack ingress endpoint: `POST /api/v1/integrations/bridge/slack/events`
- Worker command: `python manage.py run_community_bridge`
- Docker Compose service: `bridge-worker`

Deploy or restart:

```bash
docker compose up -d web scheduler bridge-worker
```

Or, if you use the included deploy script:

```bash
./deploy.sh
```

Then verify in Django admin:

- `CommunityBridgeChannel`
- `CommunityBridgeReceipt`
- `CommunityBridgeDelivery`
- `CommunityBridgeMessageLink`

## Pilot checklist

Run these checks in the mapped pilot channels:

1. Slack message appears in Discord.
2. Discord message appears in Slack.
3. Slack edit updates the Discord mirror.
4. Discord edit updates the Slack mirror.
5. Slack delete removes the Discord mirror.
6. Discord delete removes the Slack mirror.
7. Slack thread reply becomes a Discord reply.
8. Discord reply becomes a Slack threaded reply.
9. Bridge bot messages do not loop.
10. Duplicate Slack event deliveries do not create duplicate mirrored posts.
