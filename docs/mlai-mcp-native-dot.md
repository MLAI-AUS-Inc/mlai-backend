# Personal Dot on the existing MLAI MCP connection

This integration adds personal-agent permissions to `/mcp/mlai`. It preserves
`/mcp/valley`, the existing OAuth issuer and dynamic client registration route
`/mcp/oauth/register`. No second plugin, provider bearer token or static OAuth
client ID is required. A plugin/app identifier is not an OAuth client ID.

The existing OpenAI draft has an immutable `/mcp/valley` transport URL. The
explicit `VALLEY_MCP_UNIFIED_ALIAS_ENABLED` switch permits that transport to
serve freshly consented `/mcp/mlai` tokens without changing their logical OAuth
audience. The package explicitly supplies the MLAI OAuth resource and issuer
URLs. Dispatch chooses its context only from the validated token's exact
Valley/MLAI audience. Older Valley tokens keep their four tools and cannot use
native events or MCP 2.0. Anonymous Valley metadata and challenges continue to
describe the actual Valley URL and its original two scopes.

The provider must request and the member must freshly select all three scopes:

- `agent:mentions:listen`
- `agent:thread:read`
- `agent:thread:reply`

Every native tool advertises this complete atomic permission set so an OAuth
step-up can complete fresh consent. A single native scope cannot authorise the
pilot. HTTP and tool-level linking challenges include a quoted constant
`error_description`; exceptions and private identifiers are never copied into
that header.

These permissions begin unchecked in the consent UI. The member also selects
one active, signed installation created through Chat's Apps & agents flow.
Existing grants and refresh tokens never acquire personal-agent authority.
Dot-only consent uses the account revocation domain and requires no startup.
Other explicitly selected MLAI capabilities retain their existing boundaries.

The six added tools are `get_agent_account`, `list_agent_channels`,
`get_invocation`, `read_invocation_thread`, `reply_to_invocation` and
`get_invocation_status`. The account profile exposes only string fields
`id`, `name`, `email` and `nickname`, with the opaque installation UUID as `id`
and `_meta.openai/profile: true`. Use that `id` as `installation_id` and the
selected channel's resource for `agent.mentioned` subscription arguments.
MCP 2.0 discovery and Events use the same `/mcp/mlai` connection and require
matching protocol, method and operation-name headers.

Fresh consent creates a durable native grant plus an expired private adapter.
Actual successful unified bearer authentication activates the adapter. Its
random secret is never returned or published. A SQL binding identifies the
cached unified grant; it is not an independent authorisation credential.
Every native tool, poll, callback and reply checks current cached account/session
authority and the native generation. Queued records pin their exact private
credential. Cache loss, deleted bindings, revoked sessions and replaced consent
fail closed. Fresh consent revokes previous native grants, subscriptions and
queued work while leaving earlier non-agent scopes unchanged. OAuth revocation
preserves the signed installation for reconnection; installation revocation
removes it. Reconnection preserves operator budgets, and daily invocation and
reply limits apply across installation generations.

## Mentions from multiple owner devices

The mention worker scans every currently verified, unrevoked Chat device
belonging to the installation owner. A later browser sign-in must not silently
fall outside an arbitrary device limit. Each relay request still contains at
most 20 caller public keys, including the original installation device. Other
owners' devices, pending devices and revoked devices are excluded. Relay
membership checks and current-device validation still apply at admission, tool
use and reply dispatch.

Every batch starts at the same saved source cursor. The worker commits only
the smallest completed sequence across all batches, so a newer relay head or
a paginated batch cannot skip a message for an earlier caller group. Duplicate
event IDs are admitted once; the durable installation/source constraint also
deduplicates retries on subsequent polls. If any batch fails or changes epoch,
no partial admission or cursor advancement is committed. An epoch reset keeps
the existing explicit gap and pause/resume recovery behavior.

This is a code-only change: there is no new migration, permission, channel
scope or historical replay. The existing 15-minute mention lifetime and live
subscription requirement still apply. Verify a release with a fresh mention
from a verified device beyond the first 20, without passing an invocation ID
to the provider manually.

## Disabled-by-default rollout

The existing MLAI availability requirements remain in force. A deployment must
configure the existing public MCP origin and shared Redis and explicitly enable
the compatible startup/MLAI connection gates. Personal agents additionally
require `MLAI_MCP_NATIVE_AGENTS_ENABLED`, `AGENT_RUNTIME_ENABLED`,
`AGENT_RUNTIME_NATIVE_ENABLED`, `AGENT_RUNTIME_NATIVE_REPLIES_ENABLED` and
`AGENT_RUNTIME_MCP_EVENTS_ENABLED`.

Configure one trusted public channel in `AGENT_RUNTIME_NATIVE_COMMUNITIES`, a
dedicated service signer/registry credential, and the real provider setup URL
in `AGENT_RUNTIME_DOT_SETUP_URL`. A blank setup URL remains unavailable. In
unified mode the signed enrollment audience defaults to the configured existing
`/mcp/mlai` audience. The standalone configured-client gate remains unchanged
when unified mode is disabled. Human setup and private relay classification are
mounted under `/api/v1/agent-runtime/`; no second provider MCP/OAuth surface is
mounted there. The existing optional `run_agent_scheduler --native --webhooks`
worker drives profile provisioning, mention admission and bounded delivery.

The optional Compose `native-agent-worker` runs only when the native gate is
selected by deployment and participates in writer pausing and rollback. Both
GitHub Actions configuration and host stdin upserts validate the dedicated
native credentials and a single pilot community/channel. Secret values travel
through SSH stdin, never command arguments or log output. Disabling the pilot
stops and removes its optional worker.

The first live audit found that native human channel creation also needs the
existing dedicated relay-role service credential. Deployment supplies
`COMMUNITY_CHAT_ROLE_SERVICE_TOKEN` through the secret store to the existing
read-only `/api/v1/community-chat/relay-roles/<public_key>/` endpoint. The relay
holds the same value in `MLAI_CHAT_ROLE_SERVICE_TOKEN`. It must differ from the
native registry token and service signer. No admin identity or permission
fallback is used. Pilot flags can remain disabled and the channel allowlist
empty while this prerequisite is deployed and the owner creates a fresh empty
public test channel.

An installation is connected only after actual bearer authentication, listening
only after a verified live subscription, and ready only after a correlated reply
under the current grant and listening period. Historical replies do not establish
new readiness. Enrollment and fresh consent alone are not provider-connection
evidence.

## Source and verification limits

This source is prepared against backend main revision
`29aba82f58a03c51fea603b1d0c066c3a6c079b6` (release candidate rebased from the
earlier `fc985d4da97610422343294a6802bbbe6df4ed89` handoff). Main lacked `agent_runtime`; the
handoff includes that reviewed gateway source and its exact 15-model initial
migration. This integration adds no new model or migration to the native schema.
Django's model check and migration dry check report no changes.

Database-free tests run through `scripts/test_without_database.py`; their
registered PostgreSQL module deliberately skips. Real native transaction tests
were separately run on the approved disposable old-branch schema, and focused
bridge tests reuse that same schema with synthetic cached authority. Neither
test path authorises applying main's newer migration graph. The earlier
disposable-only inventory contained 340 existing migrations plus the reviewed
native initial migration. The user subsequently explicitly requested deployment
of this pilot. A read-only production graph audit at
`29aba82f58a03c51fea603b1d0c066c3a6c079b6` confirmed that every current prerequisite
is already applied; the only pending candidate is `agent_runtime.0001_initial`.
Its file SHA-256 is
`22a3cbbd144fe1c06617f24001cd9fa1ca91ab1d98d7034dc83c53a3ced20783`;
its expected canonical deployment-plan SHA-256 is
`a1ea1f419173ab4f74e8203d97b87b8c957c21de591ae0f2c85fd84ebc3365ed`.
That review does not authorise any different pending migration or claim a
production schema application. The deployed candidate must still reproduce
that exact plan before the guarded writer pause and schema transition.

These checks use synthetic providers, relays and callbacks. They do not certify
an actual Dot subscription, a production native reply, portal publication or
deployment. Those require an actual owner connection and its explicit test
mention after the compatible backend, relay and client are deployed.
