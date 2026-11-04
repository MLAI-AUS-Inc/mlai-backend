# MLAI remote MCP

The unified, separately enabled `https://api.mlai.au/mcp/mlai` resource exposes
Pulse, article workflows and native community messages to ChatGPT, Claude and
Cursor. Each platform reviews and publishes its own package/listing. A built
package or merged implementation does not establish store approval or a live
release. Store packages and reviewer instructions live in `mlai-chat/tools/mlai-plugin`.

## Compatibility and consent

`/mcp/valley`, its protected-resource metadata, public OAuth client registrations,
existing token prefixes and cache keys remain compatible. Valley's only scopes
remain `startup:brief:read` and `startup:draft:write`. Its tokens cannot authenticate
at `/mcp/mlai`, and refresh cannot change audience or permissions. Existing
connections never gain Article or Community authority through an upgrade.

Personal Dot permissions are an additional explicit opt-in on this same
connection. They begin unchecked, require a signed owned installation, and do
not expand earlier grants or refresh tokens. See [Personal Dot on MLAI MCP](mlai-mcp-native-dot.md)
for invocation-bound events, replies and disabled-by-default rollout.

The unified resource uses the same public PKCE S256 authorization server and
rotating refresh family, with an explicit resource indicator on authorization
and token exchange. A new consent requires a nonempty, explicit `scopes` list
that is a subset of the host's requested permissions. An absent OAuth `scope`
allows the new capability catalogue to be offered on the consent screen, because
Claude/Cursor native configurations do not document a requested-scope field.
Only `startup:brief:read` is selected by default in that screen; approval still
requires an explicit selected scope list. An explicit host scope constrains the
available choices. This fallback never expands an existing grant or refresh. Canonical OAuth resumes at the account-only `/connections/mcp` route; the legacy founder route remains unchanged. The browser displays requested capability
groups and lets the member opt in separately. Article and Pulse grants bind one
currently owned startup; a Community-only grant may omit the startup.

If native channel discovery is unavailable, consent keeps Pulse and Articles
available and explains why Community cannot be selected. Authentication failures
still require signing in again; unavailable channels never receive a grant.

Community consent additionally requires explicitly selected channel IDs. The
backend obtains their relay, community, channel type and verified member identity
from the trusted live relay; clients cannot choose a relay origin, owner or
wildcard. Device/session revocation, account auth-version changes, startup
ownership loss and disconnection are rechecked on calls. Owner-scoped connection
DELETE supports `grantId` to revoke one grant, or revokes the selected startup's
grants when omitted. Account-only connections use no `companyId`.

## Tools and permissions

| Scope | Tools / authority |
| --- | --- |
| `startup:brief:read` | `list_startups`, `get_monthly_update_brief`, `get_draft_status`, `get_startup_account` |
| `startup:draft:write` | `save_narrative_draft`: private narrative only, existing financial/evidence and revision safeguards |
| `article:read` | `get_article_capabilities`, `list_articles`, `get_article_run`, `get_article_draft` |
| `article:generate` | `research_article_topics`, `generate_article`: current Roo quote, integer `expectedCostPoints`, UUID idempotency key, current website/repository readiness |
| `article:draft:write` | `revise_article_draft`: bounded edits to the exact current private revision |
| `article:publish` | `prepare_article_publication`, `publish_article`: only continue the exact existing founder-approved publication handoff; never approve a draft from agent input or merge a PR |
| `community:read` | `list_community_channels`, `read_community_messages`, `search_community_messages`, `read_community_thread` |
| `community:write` | `post_community_message`, `reply_community_message`: signed and visibly attributed MCP actor, exact-channel ACL and content-sensitive UUID retry protection |

`list_startups` is also available under any selected Article/Pulse permission,
so an Articles-only connection can discover its already-authorized company ID
without receiving Pulse access. Community-only grants do not expose startup data.

Tool discovery filters by the actual grant and advertises per-tool OAuth scopes
and explicit read-only, destructive and open-world annotations. An unavailable
permission produces an MCP authentication challenge. Retrieved source documents
and community messages are untrusted data, never tool execution instructions.
No tool purchases Roo, exports credentials, rewrites verified financial evidence
or grants itself publishing approval.

Article HTTP and MCP callers share owning-domain operation services, preserving
current capability checks, quote checks, billing, dispatch and durable review
receipts. Research debit and its original request/input hash now reserve the
existing durable discovery outbox atomically, just as article dispatch does.
Retries reuse the same scoped request; changed topic/audience input fails even
if the process exited before recording the worker run. Failed delivery refunds
the original charged points rather than a newly calculated price. Status/draft reads are company/run scoped. Generation is asynchronous;
poll `get_article_run` and then read the returned scoped draft. Publication still
uses the founder review flow and existing repository integration.

See the paired [native community contract](mlai-mcp-community.md) for relay configuration and retry details.

## Native community trust boundary

The member's signing private key never reaches the backend. A separate actor key
is derived per OAuth grant from the deployment secret. The backend sends a
nonexposed, HMAC-signed ten-second, one-use delegation to the existing generic
Nostr `/query` and `/events` endpoints. The claim binds owner, actor, grant, scope,
exact channel IDs, community/relay origin, method, path and body hash. The relay
checks live owner membership and channel access, verifies the signed actor event,
and preserves normal persistence, thread counters and fan-out. DMs and arbitrary
event kinds are excluded. Stored messages carry visible agent attribution.

A successful send receipt rechecks relay access before replay. Ambiguous sends
retain the exact signed event and request identity for reconciliation; changing
content under the same ID fails. Revocation prevents subsequent calls; an already
authorized, in-flight request may finish. Rotating the shared delegation secret
invalidates delegations and changes agent keys, so reconcile pending sends before
rotation and reconnect affected Community grants.

## Release configuration

This change creates no models or migrations. Existing shared Redis and account /
startup revision schemas remain prerequisites.

- Existing `VALLEY_MCP_PUBLIC_BASE_URL` pins the API origin for both resources.
- `MLAI_MCP_ENABLED=false` is independent of the legacy Valley gate. Enable only
  after compatible backend and consent UI are released. The existing
  `COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED` gate remains a prerequisite.
- `MLAI_MCP_CLIENT_INSTALL_URLS` maps `chatgpt`, `claude`, `codex`, `cursor` to
  approved public HTTPS listings. Empty entries use manual/deep-link setup and do
  not imply approval. Legacy Valley catalogue and override semantics stay intact
  until the unified rollout is enabled.
- `MLAI_MCP_COMMUNITY_ENABLED=false` requires the compatible relay release,
  `MLAI_MCP_COMMUNITY_RELAY_URL` (one pinned HTTPS origin), and a random shared
  `MLAI_MCP_COMMUNITY_SECRET` of at least 32 bytes. Configure the matching relay
  origin and delegation secret described in its runtime configuration. Put secrets
  in the deployment secret store, never repository variables, packages or evidence.
- The backend deploy workflow passes the new booleans and public catalogue through
  validated SSH-stdin environment upserts. The delegation secret uses a GitHub
  **secret**, with no value logging. Omitted relay credentials preserve existing
  host values. Rollback can disable the new gates without deleting saved drafts.
- Preserve the existing OpenAI ownership challenge. A renamed display name does
  not authorize replacing another registration's domain proof.

After an explicitly authorized deployment, verify both audiences' discovery,
wrong-audience token rejection, fresh opt-in consent, scope-down selection,
startup isolation, revoked sessions/devices, current channel membership, retry
reconciliation, exact article quote and founder-review receipt checks. Then run
each host's real OAuth / tool scan and use its submission flow. The existing
OpenAI draft registration may retain old auth state: inspect saved server settings
and complete the scan rather than assuming package upload fixes it.

## Local validation

Use the database-free runner with explicit suites:

```sh
python scripts/test_without_database.py \
  community_chat.tests.test_startup_mcp \
  community_chat.tests.test_mlai_mcp \
  startup_updates.tests_mcp_articles_unit \
  content_factory.tests_mcp_operations_unit \
  content_factory.tests_research_dispatch_unit \
  community_chat.tests.test_mlai_mcp_community
python scripts/test_without_database.py --check-models
python -m unittest tests.test_valley_mcp_deploy_config
```

Database-free tests do not establish DB/Redis concurrency, live relay membership,
worker behavior or platform OAuth acceptance. Migrated disposable test databases
need explicit approval under `AGENTS.md`. The exact existing graph for that
approval is in `docs/mcp/disposable-test-migration-inventory.json` (363 migrations,
SHA-256 `320b08e9d2c2ef53144270dcdc4b2174157f3c4faf7fda85ccf1e3952c02d415`).
