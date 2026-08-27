# Roo Linear channel issue reader

This read-only integration lets Roo list a configured Linear queue from one
Slack channel and retrieve one issue's details and comments in follow-up
messages. The backend, rather than Roo's prompt router, owns the authorization
boundary.

## MLAI_TECH local development binding

The local Roo development app uses:

- Slack workspace: `T05N9C1QSJC`
- Slack channel: `#roo-testing` (`C0BRM181EDV`)
- Linear team: `MLAI_TECH` (`def24f5e-2990-4e28-9e06-e89db4a09f9f`)
- Linear state: `Todo` (`f3591a1e-f7a2-4514-9280-000d43ea60e5`)

Configure the backend with a Linear API key that can read that team and the
channel binding below:

```dotenv
LINEAR_API_KEY=lin_api_replace_locally
LINEAR_CHANNEL_ISSUE_BINDINGS_JSON={"T05N9C1QSJC:C0BRM181EDV":{"display_name":"MLAI_TECH · Todo","team_name":"MLAI_TECH","state_name":"Todo","linear_team_id":"def24f5e-2990-4e28-9e06-e89db4a09f9f","linear_state_id":"f3591a1e-f7a2-4514-9280-000d43ea60e5"}}
LINEAR_CHANNEL_ISSUE_MAX_COMMENTS=250
```

Production `#tech_volunteers` must be configured as a separate
`workspace_id:channel_id` entry. Its channel ID is `C0BS0J2Q3M1`:

```dotenv
LINEAR_CHANNEL_ISSUE_BINDINGS_JSON={"T05N9C1QSJC:C0BS0J2Q3M1":{"display_name":"MLAI_TECH · Todo","team_name":"MLAI_TECH","state_name":"Todo","linear_team_id":"def24f5e-2990-4e28-9e06-e89db4a09f9f","linear_state_id":"f3591a1e-f7a2-4514-9280-000d43ea60e5"}}
```

Do not reuse the `#roo-testing` ID for production authorization. If a shared
non-production backend intentionally serves both channels, the JSON object may
contain both entries; production should keep only the production entry.

Do not put the Slack bot token, signing secret, or Linear API key in source
control. No database migration is required for this feature.

## API contract

Both endpoints require Roo service authentication (`HasRooApiKey`). The caller
must forward the actual Slack workspace, channel, and requester IDs.

`POST /api/v1/integrations/linear/channel-issues/list`

```json
{
  "slack_workspace_id": "T05N9C1QSJC",
  "slack_channel_id": "C0BRM181EDV",
  "requester_slack_id": "U123",
  "limit": 50
}
```

The response contains issue identifiers, titles, links, and summary metadata
from the configured team and state. The service discards any out-of-scope node
returned by Linear.

`POST /api/v1/integrations/linear/channel-issues/detail`

```json
{
  "slack_workspace_id": "T05N9C1QSJC",
  "slack_channel_id": "C0BRM181EDV",
  "requester_slack_id": "U123",
  "issue_identifier": "TECH-16",
  "include_comments": true
}
```

The backend fetches the issue first and verifies its team before requesting
comments. A request from an unbound Slack channel, or for an issue on another
team, returns `403 linear_channel_issue_access_denied`.

Comment retrieval is paginated and bounded by
`LINEAR_CHANNEL_ISSUE_MAX_COMMENTS`. The response says when comments,
attachments, or relations were truncated so Roo can direct the user to Linear.

## Slack behavior

The first response contains numbered titles and identifiers only. A user can
then mention Roo in the same Slack thread with:

- an identifier such as `TECH-16`;
- a numbered selection such as `show me number 2`; or
- distinctive words from an issue title.

Roo returns the issue title, workflow metadata, description, labels,
attachments, relations, and comments. Slack content is escaped before it is
rendered and oversized responses are truncated with a link back to Linear.
