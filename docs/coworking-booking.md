# Roo coworking booking contract

The backend owns coworking capacity, pricing, points charging, and booking
idempotency. Roo calls `POST /api/v1/points/coworking/book/` with the Slack
member ID and requested date.

## Slack account resolution

The booking endpoint resolves identity in this order:

1. Use an MLAI account already linked to the supplied Slack member ID.
2. If none is linked, fetch that exact member's profile from the configured
   MLAI Slack workspace.
3. If the profile contains an email matching an existing MLAI account that is
   not linked to another Slack identity, persist the link and continue the
   booking against that account and its existing Roo Points balance.

The endpoint never creates an MLAI account during booking and never moves an
account from one Slack identity to another. Bot, deleted, mismatched, or
email-less profiles fail closed. A failed Slack lookup returns retryable code
`slack_identity_unavailable`; a verified profile without a safe existing match
returns terminal code `slack_account_not_linked`.

An account created earlier by a Points Admin award is already bound to the
target Slack member ID, even when it uses a placeholder email and the member
has never run `link`. Coworking booking reuses that points owner directly and
charges its existing balance. It does not require email linking or create a
second account.

Roo's self-service `link` command starts a short-lived, one-time capability with
`POST /api/v1/users/slack-founder-link/start/` using Roo's dedicated service
key and the actor from the verified Slack event. Roo delivers the capability
only in a direct message. The member signs in to Founder Tools to preview and
complete the link, so email matching is not an ownership authority. Creating a
fresh request invalidates older unused capabilities; completed capabilities are
single-use. Existing direct or explicit ownership conflicts fail closed rather
than silently moving either identity.

Linking is a durable identity update. If linking succeeds but the booking is
rejected (for example, because the account has insufficient points), a later
retry reuses the link. The booking service's existing per-user/date idempotency
key ensures a response-loss retry does not charge the account twice.

## Roo-Founder Tools account-link API

All four endpoints return `Cache-Control: no-store` and `Pragma: no-cache`.
Tokens are opaque 43-character URL-safe bearer capabilities. Clients must not
log them or forward them to any endpoint other than `preview` or `complete`.

### Start a link

`POST /api/v1/users/slack-founder-link/start/` requires Roo's strict service
API key and does not accept browser authentication. The request body is
`{"slack_user_id": "U123..."}`.

- `201`: `{"status":"link_required","link_url":"https://mlai.au/founder-tools/link-roo?token=...","expires_at":"<ISO-8601>"}`
- `200`: `{"status":"already_linked"}`
- `400 invalid_request`: `slack_user_id` was omitted
- `404 slack_user_not_found`: the verified Slack identity has no usable MLAI account
- `409 link_conflict`: an account already belongs to a different connection
- `429 link_rate_limited`: includes `retry_after_seconds` and `Retry-After`
- `503 slack_identity_unavailable`: Slack identity verification was unavailable

Creating a successful new request invalidates the actor's older unused
requests. It does not invalidate a completed connection.

### Verify deployment compatibility

`GET /api/v1/users/slack-founder-link/health/` requires Roo's strict service
API key and returns `200 {"status":"ok","contract":"slack-founder-link-v1"}`.
It performs no identity lookup or mutation. Roo deployment must verify this
exact contract before enabling the user-facing linking action.

### Read connection status

`GET /api/v1/users/slack-founder-link/status/` requires the normal authenticated
Founder Tools browser session. It returns no raw Slack identifier:

```json
{
  "status": "connected",
  "connection_type": "explicit",
  "can_link_separate_account": false,
  "slack_display_name": "Display name",
  "verified_at": "<ISO-8601 or null>"
}
```

`status` is `connected` or `not_connected`; `connection_type` is `explicit`,
`direct`, or `null`.

### Preview a capability

`POST /api/v1/users/slack-founder-link/preview/` requires the authenticated
Founder Tools browser session and body `{"token":"..."}`. A successful `200`
returns `status` (`ready`, `already_linked`, or `already_connected`),
`slack_display_name`, and `expires_at`. Preview never consumes the capability.

### Complete a capability

`POST /api/v1/users/slack-founder-link/complete/` uses the same authentication
and request body as preview. It returns `201 {"status":"linked"}` when it
creates the verified connection, or `200` with `already_linked` or
`already_connected` for an idempotent existing connection. Successful
completion consumes the capability and records the authenticated Founder Tools
user that consumed it.

Preview and complete share these terminal errors:

- `400 invalid_token`: malformed, unknown, or invalidated capability
- `409 token_already_used`: includes boolean
  `connection_matches_requesting_user`; only `true` permits the browser to
  present the prior completion as belonging to the current user
- `409 link_conflict`: either identity belongs to another verified connection
- `410 expired_token`: capability expired before completion

Clients must treat unrecognised success bodies as commit-uncertain and verify
status before attempting another completion.
