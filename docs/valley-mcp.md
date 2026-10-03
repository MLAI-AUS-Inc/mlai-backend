# Valley remote MCP for monthly updates

This implementation is local source, disabled by default. It lets a founder's
already-connected agent read a startup brief and submit a private narrative draft
for review in MLAI Chat. The agent uses its own Gmail, Linear, Luma and other
connections. Valley's direct Xero/Stripe connectors remain the authority for
financial fields. The server exposes no publishing, financial write or provider
credential tools.

## Connection flow

1. MLAI Chat fetches company-scoped `GET agent-connection/` under
   `/api/v1/community-chat/startups/` and displays the returned client catalog.
2. Claude's documented custom connector URL opens its prefilled add-connector
   dialog. Cursor's documented HTTPS install wrapper opens its MCP installer.
   The picker combines the OpenAI clients into one **Codex** option. Codex uses a
   configured published listing when one exists; before
   publication, the UI presents the server URL and platform setup instructions.
   Codex opens its official MCP setup guide: Settings → MCP servers → Add server
   → Streamable HTTP → URL → Save/Restart → Authenticate.
   Launching a client does not imply successful connection.
3. The client discovers the protected resource and authorization server, then
   registers a public client or uses an explicitly configured existing client.
4. `/mcp/oauth/authorize` validates exact registered redirect URI, `resource`,
   state, scopes and PKCE S256. It redirects to
   `${COMMUNITY_CHAT_FRONTEND_URL}/my-startup/connections?mcpAuthorization=<opaque>`.
   Chat's existing account sign-in preserves this query parameter. No access
   credential is carried in this URL.
5. Chat fetches `agent-connection/authorization/<opaque>/`, shows the claimed
   client name, credential-free registered callback origin, scopes and the founder's startups, and asks the founder to choose
   one startup. Its authenticated POST sends `{requestId, approve: true}` with
   `company_id` and receives the client's registered callback in `redirectUrl`.
   Denial sends `approve: false` and requires no startup selection.
6. The callback contains a short-lived one-use authorization code, state and
   RFC 9207 issuer. The client exchanges the code and PKCE verifier for scoped
   OAuth tokens. It then calls the Streamable HTTP MCP endpoint.

Consent is necessary to authorise the external agent. Connections reuse the
normal Chat sign-in rather than introducing another account system. A founder
can disconnect all grants for the selected startup with `DELETE agent-connection/`.
A company-wide revocation epoch is checked on every request, so disconnection
revokes even a grant missing from the cached connection-list index. Account
session revocation/expiry, device ownership changes, account auth-version
changes and removal of startup ownership also revoke effective access.

## HTTP contract

| Route | Method | Purpose |
| --- | --- | --- |
| `/.well-known/openai-apps-challenge` | GET | Exact public OpenAI plugin ownership token; no OAuth or startup data |
| `/mcp/valley` | POST | Stateless JSON Streamable HTTP: initialize, ping, tools/list, tools/call |
| `/.well-known/oauth-protected-resource/mcp/valley` | GET | Protected resource metadata (also available without the path suffix) |
| `/.well-known/oauth-authorization-server` | GET | OAuth authorization server metadata |
| `/mcp/oauth/register` | POST | Public PKCE client registration; no client secret |
| `/mcp/oauth/authorize` | GET | Validate OAuth request and resume through Chat sign-in |
| `/mcp/oauth/token` | POST | Code exchange or rotating refresh-token exchange |
| `/mcp/oauth/revoke` | POST | Revoke the grant associated with the caller's token/client |

MCP requests send `Authorization: Bearer <OAuth access token>`, JSON Content-Type,
and Accept containing both `application/json` and `text/event-stream`. The server
returns JSON; it does not allocate a session or provide an SSE stream. GET/DELETE
on the MCP route return 405. Supported protocol versions are 2025-11-25,
2025-06-18 and 2025-03-26. Incoming browser Origin values must match the explicit
MCP allowlist; server-to-server calls without Origin are accepted. Existing
throttling uses the shared cache.

The authenticated setup response contains `enabled`, `available`, `reason`,
`mcpUrl`, `authorizationUrl`, `connection.connected`, `connection.grants` and
`clients`. Each client contains `id`, `name`, `installUrl`, `setupUrl`, `method`,
`instructions` and public `config`. Directory URLs must be real published URLs;
there is no invented ChatGPT/Codex listing or success state.

## Tools and evidence

Startup company IDs are the existing founder-company UUIDs. OAuth consent,
ownership checks, tools and connection indexes use the same canonical UUID;
alternate UUID spelling cannot create a separate disconnection epoch. Draft,
revision and user IDs retain their existing integer types. The public ownership
challenge always returns its fixed plain-text representation, independently of
the client's Accept header, while retaining throttling and method restrictions.

- `list_startups`: return only the explicitly granted startup.
- `get_monthly_update_brief`: return startup context, selected calendar month,
  timezone-aware reporting period and narrative-writing instructions.
- `save_narrative_draft`: accept supported narrative sections, source references,
  coverage notes and a UUID `requestId`. Edits also provide `updateId` and the
  current `expectedRevision`. The first request's UUID is the existing independent
  draft `creation_key`; receipts in the versioned memo reject changed-content
  retries and preserve idempotency across ordinary retries.
- `get_draft_status`: return scoped draft/revision identity and its private review
  URL.

Save uses existing `resolve_update`, `capture_snapshot` and `save_revision`
services. Existing edits retain the exact financial snapshot. New drafts capture
server-owned financial evidence. Top-level and nested narrative/source schemas
are allowlisted; incoming metrics, charts, financial snapshots, audience changes
and publishing commands are rejected. All MCP saves are private drafts with
`agent_supplied` provenance. New drafts and edits of passed or already classified
agent revisions require `needs_review` validation. Existing unresolved Valley
validation is preserved in full when the import carries those claims forward.
The existing founder
`reviewed: true` action can approve the exact revision of these external narrative
assertions. That narrow acceptance never bypasses other pending/failed Valley
verification, changes financial evidence or adds an MCP publishing tool. Its
human approval receipt preserves the external sources' unverified classification. References are external
agent assertions, not provider-verified observations. The private Chat update DTO
includes `agentProvenance` and `agentSources`; public/community DTOs omit them.

A founder still reviews financial claims in free text. The MCP cannot write the
verified financial block. Founders review and approve exact saved revisions
through the existing Chat flow.

## Configuration and rollout

No model changes or migration files are introduced. This reuses the reporting
revision schema and revocable account-session schema already required by startup
updates. Production security state uses the existing shared Redis cache.

- `COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=true`: prerequisite feature gate.
- `VALLEY_MCP_ENABLED=true`: separate MCP rollout gate; defaults false.
- `VALLEY_MCP_PUBLIC_BASE_URL`: public HTTPS API origin, without path; the resource
  is `${origin}/mcp/valley`. It is never inferred from an untrusted Host header.
- `VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN`: exact public challenge token issued by
  OpenAI for this plugin. The origin-root challenge route returns plain text and
  works before the MCP feature gates are enabled. Empty or malformed values
  return 404. This is an ownership proof, not an OAuth credential. Before setting
  it, check that the host is not serving another plugin's challenge; do not
  concatenate tokens or overwrite another active proof. After deployment, confirm
  the exact body and use **Verify Domain** in the OpenAI MCP connection drawer.
- `VALLEY_MCP_ALLOWED_ORIGINS`: comma-separated exact Origins for browser clients
  calling the MCP server directly. Backend clients ordinarily send no Origin.
- `VALLEY_MCP_CLIENT_INSTALL_URLS`: JSON mapping of client id to an approved public
  listing URL; keys `claude`, `codex`, `cursor`. A legacy `chatgpt` entry is used
  for Codex when no `codex` URL is configured; it never creates a second picker
  option. An explicit `codex` URL takes precedence.
- `VALLEY_MCP_OAUTH_CLIENTS`: optional JSON map keyed by known public client ID,
  with `client_name` and exact `redirect_uris`. Dynamic registration also works.
- `REDIS_URL`: existing production prerequisite. MCP refuses process-local cache
  in production. Cache keys contain hashes of access/refresh tokens and codes;
  raw credentials are returned only at token issuance.

The deployment workflow takes the public base, MCP rollout gate and ownership
token from repository variables with the same names. It defaults the MCP gate to
false and the base to `https://api.mlai.au`; enabling requires an explicit variable
change after the compatible consent UI is available. Omitted startup-gate and
challenge variables preserve the host's existing values. All supplied values are
validated before host changes, travel over SSH stdin, and leave Redis and unrelated
settings intact. The host upsert refuses to replace a differing ownership token;
reconcile an existing plugin's proof explicitly before changing it.

Authorization intents expire after ten minutes, authorization codes after five
minutes, access tokens after one hour and grants/refresh tokens after at most
thirty days. Refresh tokens rotate once; correct-client reuse of a spent token
revokes its grant and every rotated descendant. Only hashed token-family linkage
is retained until the grant expires. Unknown tokens and wrong-client attempts
cannot revoke that family. Account-session expiry may end a grant
earlier. Cache loss fails closed and requires reconnection. Redis needs adequate
capacity and the normal operational durability settings; this is revocable
connection state, not the durable draft store. Roll back by disabling
`VALLEY_MCP_ENABLED`; existing draft revisions remain in the database.

Public OAuth client metadata has no cache expiry: OpenAI hosts register a client
once per MCP connection and reuse its ID for later authorisation, including after
the thirty-day grant expires. Retaining client metadata does not extend any
grant, account session, access token or refresh token. These small records contain
the public client ID, name and validated callback URLs, never a client secret or
user authorisation. Registration uses the existing anonymous 120/minute throttle,
but records accumulate across connections; monitor Redis capacity and registration
volume rather than automatically expiring active clients. Non-expiring keys still
depend on Redis persistence and eviction policy. A flush, eviction or unrecoverable
Redis loss removes registered clients and fails closed with `invalid_client`;
the host must then register a new connection. Disconnection revokes grants without
deleting shared client metadata. See [OpenAI's client-registration lifecycle](https://developers.openai.com/plugins/build/auth).

Before enabling, deploy the compatible Chat/backend changes, configure the public
HTTPS origin and callback client configuration, and complete platform acceptance:
actual Claude install/consent/tool call, Cursor install/consent/tool call and
Codex MCP authentication/tool call. If the same OpenAI listing is also offered in
ChatGPT, test custom-connector OAuth/tool calls on an eligible account. Test fresh login
resume, no-company denial, cross-startup access, revoked account/session,
disconnection, modified retry content, revision conflict and financial-field
rejection. Verify server URL/metadata are publicly reachable through the ingress.
The local database-free tests do not establish provider sign-in or database
integration success. Database-backed tests require the repository's specific
migration approval before constructing a test database.

For the screenshot-style OpenAI directory installation, deploy the MCP before
requesting approval: reviewers need a working public HTTPS resource, public OAuth
discovery and a functioning sign-in/consent callback. The API host alone being
live is insufficient. Submit the deployed MCP in an OpenAI plugin package using
the current publisher portal and replace Codex's setup fallback with its assigned,
approved listing URL. Keep an approved shared ChatGPT/Codex listing under the
`codex` key. Claude custom connector prefilling and
Cursor installation do not require a public directory listing. A directory
submission/review is a separate release action; no submission or deployment was
performed by this implementation.

## Local validation

```sh
python scripts/test_without_database.py community_chat.tests.test_startup_mcp
python scripts/test_without_database.py --check-models
```

The focused suite covers protocol initialization/discovery, scoped tool catalogs,
PKCE/resource/callback checks, code/refresh replay, no-company denial, account and
device revocation, ownership isolation, financial-field rejection, exact revision
matching, retry identity and selected-company disconnection. The second command
performs Django system and migration-drift checks without a database connection.

## Platform references

- [Claude directory versus custom connectors](https://claude.com/docs/connectors/building/directory-vs-custom)
- [Cursor installation deep links](https://cursor.com/docs/reference/deeplinks)
- [OpenAI MCP and connectors](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
