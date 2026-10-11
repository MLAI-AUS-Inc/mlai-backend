# Member agent gateway and Hermes worker API

Source implementation, 5 October 2026. The application is registered, but the
runtime, Slack reads, message delivery, public web access, and MCP webhooks each
default to disabled. This is not a deployment or a reactivation of the native
Buzz/MLAI Chat agent rollout. The initial schema still requires the separate
[migration review](agent-runtime-migration-proposal.md).

## Identity and consent

The canonical principal is the existing MLAI user. An installation has one
owner and a separate expiring grant containing exact capabilities and public
Slack resources (`slack:T…:C…`). Administrative roles confer no additional agent
authority. Effective access intersects current account state, grant generation,
credential scope, operator workspace/channel allowlists, and the owner's live
Slack user membership. Private channels, DMs, Slack Connect, archives, and native
relay resources are excluded.

The backend's existing personal `ExternalServiceConnection` is the Slack source.
Each bounded provider request locks the canonical user against OAuth disconnect,
checks the current OAuth generation, validates `auth.test` against the stored
workspace/user identity, and checks the public channel's `is_member` state.
An organization connection or Roo bot credential cannot substitute. Expired
Slack credentials require reconnection. No private mirror cache is read.

`GET /api/v1/agent-runtime/connected/` is the owner management UI. It uses an
existing MLAI account session (Chat account cookies or existing platform JWT).
Cookie-authenticated writes require a configured, exact origin. The UI creates
Hermes grants, queues/cancels tasks, adds/stops responsibilities and channel
monitors, shows outcomes and downloadable artifacts, reviews exact action drafts,
and revokes access. The stop-all control revokes every active owned installation,
including installations beyond the first 100 displayed grants. Grant expiry is
limited to 30 days for enrollment. The active website's platform dashboard links
to it through authenticated `/platform/agents` when the separate build-time
`VITE_AGENT_RUNTIME_ENABLED` flag is enabled. Both enrollment forms use an
eligible-channel picker backed by `GET channels/`; no manual source IDs are
required. OAuth sign-in handoff preserves only the fixed authorization fields.

BYO clients use authorization code + S256 PKCE through
`oauth/authorize/` and `oauth/token/` under that prefix. Configure explicit public
clients in `AGENT_RUNTIME_OAUTH_CLIENTS`:

```json
{
  "my-agent": {
    "name": "My agent",
    "redirect_uris": ["https://my-agent.example/oauth/callback"],
    "scopes": ["messages:read", "events:read", "tasks:read", "tasks:write"]
  }
}
```

Redirects match exactly; only HTTPS or explicitly registered numeric loopback
HTTP callbacks are accepted. Authorization requires `response_type=code`,
`code_challenge_method=S256`, `state`, and an exact `resource` audience. Code
exchange additionally requires `client_id`, `code`, `redirect_uri`,
`code_verifier`, and `resource`. Refresh requires `grant_type=refresh_token`,
`client_id`, `refresh_token`, and the same `resource`; optional `scope` can only
narrow permissions. Codes expire after three minutes and are single use. Access
tokens last at most 15 minutes; refresh credentials rotate, and replay revokes
the entire credential family. Only hashes are persisted. A disabled client or
narrowed client scope invalidates its access credentials.

Public discovery lives at `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource`. There is no dynamic client registration,
upstream token passthrough, or support for arbitrary client metadata URLs in this
pilot. OpenAI/Hermes interoperability still needs testing with their actual
registered clients before enabling a rollout.

## Tools and events

The audience-bound agent access token works on `POST tools/` with
`{"tool":"read_thread","arguments":{"resource":"slack:T…:C…","thread_id":"123.456"}}`.
Tools are `get_profile`, `list_channels`, `read_channel`, `read_thread`,
`search_messages`, `get_events_since`, and task-only `propose_action`.

All reads are bounded. Search filters one authorized channel-history page; it
does not claim to search the complete archive. A task credential is additionally
bound to the task's one selected resource. It cannot create/enumerate other
tasks or reuse a broader parent grant to read another channel. Message bodies
are marked untrusted and omit hidden provider metadata and files.

`get_events_since` accepts an exact `resource`, an opaque `cursor` (empty for a
first page), and `limit` up to 100. Cursors are encrypted and bound to the grant,
generation, and resource. Event payloads contain identifiers, never message
bodies. Source identity and resource commit ordering support deduplication
without skipping a late-committing event.

`POST mcp/` supports stateless legacy MCP initialization (2025-03-26,
2025-06-18, 2025-11-25) and MCP 2.0 `server/discover` (2026-07-28). Modern
catalogue responses declare private caching with zero TTL. Both protocol paths
call the same broker. The optional OpenAI event extension is advertised only
when `AGENT_RUNTIME_MCP_EVENTS_ENABLED=true`. `events/list`, `events/subscribe`,
and `events/unsubscribe` use separate verified webhook subscriptions; callbacks
and signing secrets are encrypted in server-only subscription delivery state.
These subscriptions are not exposed as task inputs or ordinary user-editable
delivery parameters. See [OpenAI's MCP Events contract](https://developers.openai.com/plugins/build/mcp-events).

## Owner control routes

All routes below use `/api/v1/agent-runtime/` and current human account auth:

| Route | Behavior |
| --- | --- |
| `GET/POST connected/` | List/manage grants or enroll a managed agent; JSON listing uses `?format=json`. |
| `GET channels/` | List eligible pilot channels using the current owner's Slack authority. |
| `POST grants/{grant}/revoke/` | Fence credentials, tasks, subscriptions, and pending actions. |
| `POST installations/{installation}/revoke/` | Disable the installation and all its grants. |
| `GET/POST grants/{grant}/tasks/` | Read tasks/results or enqueue a bounded task with a client idempotency key. |
| `POST tasks/{task}/cancel/` | Cancel owned work and fence its active lease. |
| `GET/POST grants/{grant}/responsibilities/` | Inspect/create interval or one-shot work with timezone and quiet hours. |
| `POST responsibilities/{id}/stop/` | Stop future scheduling. |
| `GET/POST grants/{grant}/subscriptions/` | Inspect/create future-event task triggers, with exact resource/event types. |
| `POST subscriptions/{id}/stop/` | Stop future event deliveries/tasks. |
| `GET/POST actions/{action}/` | Review/approve exact hashed content and destination, or reject. JSON GET uses `?format=json`. |
| `GET artifacts/{artifact}/` | Download as an attachment after current grant/source checks. |

External agent access tokens can call `GET/POST tasks/` with their own grant's
`tasks:read` or `tasks:write` scope. Task model/tool credentials cannot use this
control API. Human task results and artifacts also recheck current resource
access and captured generation; grant revocation is not bypassed by stored data.

## Worker and source ingress

Worker routes use a distinct `AGENT_RUNTIME_WORKER_KEY` of at least 32 characters:

- `POST internal/tasks/claim/`: `{worker_id}` returns `{task:null}` or task data,
  current lease token/epoch/expiry, instruction/inputs/limits, fixed configured
  model, and separate task tool/model credentials. Public web capability is true
  only with operator enablement **and** an explicit `web:access` grant.
- `POST internal/tasks/{task}/heartbeat/`: `{lease_token,lease_epoch}` rechecks
  grant and source access before extending a lease.
- `POST internal/tasks/{task}/complete/`: lease fields, `result` object, optional
  nonnegative reported cost, and at most ten artifacts containing `name`,
  `sha256`, `content_base64`, and optional `media_type`. All artifact rows and the
  result commit together; exact retries reuse the completion receipt. Conflicting
  retries fail. Raw data stays in configured private filesystem storage.
- `POST internal/tasks/{task}/fail/`: lease fields, stable `error_code`, and
  explicit `retryable` flag. Optional `details` is a bounded JSON object (16 KiB)
  accepted only for `approval_required`; those failures are terminal and displayed
  as review data. No command approval/resume executor is enabled. Completed,
  expired, or cancelled leases cannot be reused.

`POST internal/tasks/admit-slack/` uses its own `AGENT_RUNTIME_ADMISSION_KEY`,
separate from event ingestion and worker authority. The trusted Roo wrapper calls
it only for a signature/replay-verified `app_mention` that exactly begins
`<@ROO_BOT_ID> run: ` followed by the member's instruction. It accepts only
`team_id`, `user_id`, `channel_id`, `source_event_id` (`slack:T…:Ev…`), and
`instruction`. It resolves one canonical member from current personal Slack
OAuth connections, requires exactly one eligible Hermes grant for the channel,
and rechecks the exact Slack user and live channel membership. Missing or
ambiguous identities/grants fail closed; caller-supplied owner/grant IDs, limits,
and credentials are rejected. Operators should provision one managed Hermes
grant per member/channel for this command path.

The 202 receipt is `{task_id,status,new,owner_url}`. A repeated source event
returns the original task with `new:false`, including after grant replacement;
Roo uses this flag to avoid duplicate acknowledgements. Public task creation
cannot use the reserved `slack-command:`, `schedule:`, or `event:` idempotency
namespaces. Service ingress alone cannot enlarge the member's grant.

Artifact aggregate budget is enforced by the task. The route additionally caps
each decoded file at 10 MiB and the JSON envelope at 15 MiB. The existing Django
`DATA_UPLOAD_MAX_MEMORY_SIZE` default is 10 MiB: deployers must account for base64
overhead (or use smaller artifacts), and set matching proxy/body limits before
advertising larger uploads. Artifact storage is never a public media URL.

Task model tokens work only on `model/v1/chat/completions`; provider credentials
remain in the backend. Fixed model selection, current source access, lease and
grant generation, model-call/token/cost budgets, and response bounds apply.
Input/output prices must be configured as positive microcurrency units per
million tokens. No arbitrary provider URL or remote media input is accepted.

`POST internal/events/` uses a separate `AGENT_RUNTIME_EVENT_KEY`. It accepts:

```json
{
  "source": "slack",
  "source_event_id": "slack:T123:Ev123",
  "resource": "slack:T123:C123",
  "event_type": "message.created",
  "occurred_at": "2026-10-05T10:00:00Z",
  "payload": {"team_id": "T123", "channel_id": "C123", "message_ts": "123.456"},
  "actor_kind": "human"
}
```

Only signature/replay-verified trusted source ingestion may call this route.
Additional payload fields are restricted to thread/author/client-message IDs and
subtype. Known action receipts/client-message IDs are classified as agent output
even if provider metadata is absent. Agent-origin events do not wake agents by
default. Trusted ingress is not an arbitrary member message injection API.

Message sending has another independent flag and needs `messages:write`, an
exact owner approval, current Slack `chat:write` user consent, and the trusted
outbox scheduler. Approved text includes immutable visible agent attribution.
The adapter adds `mlai_agent_action` metadata and a stable client message ID,
disables automatic SDK retries, and records a receipt. Ambiguous sends require
reconciliation rather than automatic resend.

## Validation

Database-free checks use `scripts/test_without_database.py` with explicit
`agent_runtime.tests` modules. They replace environment credentials, block
network connections, and reject database access. Provider operations, actual
client OAuth flows, PostgreSQL concurrency, runtime egress, migrations and
deployment require their separate approved verification stages.
