# Native MLAI community MCP

The unified MLAI MCP exposes native community channels through the existing
relay Nostr `/query` and `/events` doors. It does not use the Slack bot, an
administrator's content credentials, or a human signing private key.

## Consent and authority

`community:read` enables `list_community_channels`, `read_community_messages`,
`search_community_messages`, and `read_community_thread`.
`community:write` enables `post_community_message` and
`reply_community_message`. These scopes are separate opt-ins on a new MLAI
grant; legacy Valley grants do not acquire either scope.

Consent resolves the current account session's **verified** native device. A
metadata-only relay query lists that device's live joined stream/forum rooms.
The member selects between one and 100 exact channel UUIDs. The server stores
the relay origin, community UUID, channel type and owner public key with each
resource; neither wildcard selection nor caller-supplied authors/hosts are
accepted. Direct messages are excluded. Read/write permission requires live
relay and channel membership, including for public rooms and relay admins.

Each tool resolves the account session/device and validates its current grant,
under the same user-first transaction lock used by device/account revocation.
The relay independently reads current membership, bans and write timeouts for
every operation. Removed members cannot retain access through a cached channel
listing. Archived rooms remain readable but cannot receive a message.

Disconnect prevents subsequent operations. An operation already authorised and
in flight can complete, as with existing account logout. Revocation cannot
recall a message already accepted by the relay.

## Delegated Nostr transport

The gateway emits `X-MLAI-MCP-Delegation` and `X-MLAI-MCP-Signature` headers.
The former is unpadded base64url JSON; the latter is HMAC-SHA256 over
`mlai-mcp-delegation-v1:` followed by the exact encoded header. Claims bind:

- version, owner and distinct agent public keys, grant ID and operation scope;
- exact channel UUIDs and the host-resolved community UUID;
- the deployment-pinned HTTPS relay origin, POST method and exact path;
- SHA-256 of the exact request body, issuance and expiry, and a random nonce.

The lifetime is at most ten seconds. The relay verifies the HMAC in constant
time and uses its shared Redis `Nip98ReplayGuard` for atomic nonce replay
rejection across pods. Missing authority configuration or Redis errors fail
closed. Delegations and signing keys are never returned to the MCP client.

Consent queries are exactly
`[{"kinds":[39000],"mcp_channels":true,"limit":100}]`; they return bounded,
joined-room metadata only. Other queries require explicit `kinds` and `#h`
filters, exact selected channels, supported message kinds and a maximum 100
results per filter. Native member filtering and private-reader restrictions
remain in the relay's existing query engine.

Posts are signed with a separate grant-derived agent key. The relay verifies
the event signature and its signed `h`, `mlai-mcp` owner/request and `client`
tags. The visible message ends with assistant attribution. Only additive
stream messages, forum posts and forum comments are accepted; profile changes,
administration, membership, edits and deletion are outside this delegation.
Every thread reference must resolve to a live message in the same room.
The normal ingest path still performs persistence, thread counters and realtime
fan-out. Agent ownership uses the existing immutable native agent relationship.

## Send retries

Writes require a UUID `requestId`. The gateway records the content hash and
exact signed event before sending. A same-content retry queries that exact
event under current ACLs, reconciling a lost acknowledgement before resending
the same event. Changed-content reuse fails. The signature/event ID never
changes on retry, so transport ambiguity cannot create a second message.
An old or deleted event requiring reconciliation is reported for review in
MLAI Chat; the gateway does not manufacture a new message ID.

## Paired configuration

Backend configuration:

- `MLAI_MCP_COMMUNITY_ENABLED=false` by default.
- `MLAI_MCP_COMMUNITY_RELAY_URL`: one explicit HTTPS relay origin.
- `MLAI_MCP_COMMUNITY_SECRET`: a dedicated secret of at least 32 bytes.

Relay configuration:

- `MLAI_MCP_RELAY_URL`: exactly the same HTTPS origin.
- `MLAI_MCP_DELEGATION_SECRET`: exactly the same dedicated secret.

Absent relay settings disable this authority. Partial or weak settings fail
relay startup. Existing membership/role service credentials are not reused.
The relay's protected production deployment reads `MLAI_MCP_RELAY_URL` from a
GitHub production environment variable and `MLAI_MCP_DELEGATION_SECRET` from
the matching environment secret. Its existing mode-0600 SSH environment
renderer passes the pair to the relay container; sidecars and migration
containers explicitly clear them. Production validation requires the canonical
`https://chat.mlai.au` origin and rejects reuse of existing service credentials.
See the chat repository's `deploy/mlai/README.md` for the provisioning path.
No model or migration changes are required. This implementation does not
establish deployed runtime or provider/store acceptance.

## Local validation

```sh
python scripts/test_without_database.py community_chat.tests.test_mlai_mcp_community
```

Relay tests in `api::mcp_delegation::tests` cover host/body/path/lifetime binding,
supported query schemas, consent isolation, distinct signed attribution and
membership policy. The two ignored live tests need local test Postgres and
Redis; they prove that a relay admin without private-room membership is denied,
distinctly signed native posts/replies retain persisted thread counters, the
actor acquires no channel membership, and removing the owner denies the next
delegated operation despite a warmed channel cache. An explicit
`BUZZ_TEST_DATABASE_URL` override accepts loopback test databases only.

```sh
cargo test -p buzz-relay --lib api::mcp_delegation::tests
BUZZ_TEST_DATABASE_URL=postgres://buzz@127.0.0.1:5432/buzz \
  REDIS_URL=redis://127.0.0.1:6379 \
  cargo test -p buzz-relay --lib api::mcp_delegation::tests::live_ -- --ignored
```
