# Roo Linear channel issues

This integration lets Roo read and, when explicitly enabled, create and edit MLAI_TECH
Linear issues from one configured Slack channel. The backend, rather than
Roo's prompt router, owns the authorization boundary.

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
LINEAR_CHANNEL_ISSUE_LIST_RATE=60/minute
LINEAR_CHANNEL_ISSUE_DETAIL_RATE=20/minute
LINEAR_CHANNEL_ISSUE_STATUSES_RATE=30/minute
LINEAR_CHANNEL_ISSUE_WRITE_RATE=10/minute
LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=false
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

## Production deployment configuration

The backend deployment reads `LINEAR_API_KEY` from a GitHub Actions repository
secret for both read and write requests. It reads
`LINEAR_CHANNEL_ISSUE_BINDINGS_JSON`, `LINEAR_CHANNEL_ISSUE_WRITES_ENABLED`, and
`LINEAR_CHANNEL_ISSUE_MAX_COMMENTS` from repository variables. The deployment
validates all three values before connecting to the production host, then
installs them into the host `.env` over SSH stdin before recreating services.

Adding the GitHub settings does not deploy the feature by itself. The normal
reviewed deployment workflow consumes them only after this deployment wiring
has reached `main`.

## API contract

Read endpoints require Roo service authentication (`HasRooApiKey`). The write
endpoint requires the stricter `HasStrictRooApiKey`. The caller
must forward the actual Slack workspace, channel, and requester IDs.

`POST /api/v1/integrations/linear/channel-issues/list`

```json
{
  "slack_workspace_id": "T05N9C1QSJC",
  "slack_channel_id": "C0BRM181EDV",
  "requester_slack_id": "U123",
  "limit": 50,
  "status": "all"
}
```

The response contains issue identifiers, titles, links, and summary metadata
from the configured team. With no `status`, the configured state remains the
default; use a live status name or `all` to change the filter. The service
discards any out-of-scope node returned by Linear.

`POST /api/v1/integrations/linear/channel-issues/statuses` returns the live
workflow states belonging to the bound team.

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

The backend fetches the issue first and verifies its team and that it is not
archived before requesting comments. Issues remain readable after moving out
of the default `Todo` state.

`POST /api/v1/integrations/linear/channel-issues/write` applies one explicit
typed operation. It requires workspace, channel, requester, issue identifier,
operation, value, Slack request ID, and the issue's exact previously-read
`updatedAt`. The backend re-fetches and re-authorizes the issue immediately
before one non-retrying mutation. A bounded shared-cache receipt rejects a
duplicate Slack delivery without requiring a database table. Supported
operations are comments; title;
description append/replace; priority; estimate; due date; assignee; labels;
project; cycle; status; and duplicate relation. Team moves, archive, deletion,
existing-comment changes, and arbitrary GraphQL are not accepted.

`POST /api/v1/integrations/linear/channel-issues/create` immediately creates
one issue in the channel-bound team. The caller supplies workspace, channel,
requester, Slack request ID, and an explicit title. Description and status are
optional; the configured queue status is used by default. The backend derives
`teamId` exclusively from the binding and rejects caller-supplied team fields.
It also accepts optional priority, estimate, due date, assignee, labels,
project, and cycle values for trusted clients, resolving every named target
against the bound MLAI_TECH catalog. The same cache receipt and non-retrying
write transport prevent duplicate creation from Slack redelivery.

If `updatedAt` changed, the endpoint returns `409`. If Linear may have accepted
a mutation but its transport response is uncertain, the endpoint returns
`502 linear_channel_issue_write_uncertain`; Roo must tell the user to inspect
Linear and must not retry automatically.

Comment retrieval is paginated and bounded by
`LINEAR_CHANNEL_ISSUE_MAX_COMMENTS`. The response says when comments,
attachments, or relations were truncated so Roo can direct the user to Linear.
Requests are additionally protected by separate Redis-backed scoped throttles:
`LINEAR_CHANNEL_ISSUE_LIST_RATE` for list calls and
`LINEAR_CHANNEL_ISSUE_DETAIL_RATE` for the more expensive detail calls, plus
separate status and write limits.

## Slack behavior

The first response contains numbered titles and identifiers only. A user can
then mention Roo in the same Slack thread with:

- an identifier such as `TECH-16`;
- a numbered selection such as `show me number 2`; or
- distinctive words from an issue title.

Roo returns the issue title, workflow metadata, description, labels,
attachments, relations, and comments. Slack content is escaped before it is
rendered and oversized responses are truncated with a link back to Linear.
