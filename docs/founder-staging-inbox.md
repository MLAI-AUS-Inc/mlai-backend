# Founder staging inbox component

This operator-owned component implements the inbox contract consumed by
`mlai-chat/desktop/tests/staging/founder-journey.spec.ts`. It is not connected to
any running environment by default. Implementing this component does not prove
signup, account creation, normal outbox delivery, article generation, preview,
export, or the full seven-screen staging journey.

## Contract

The configured email domain must contain a literal `staging` label. Only
`founder-acceptance-<canonical UUIDv4>@<exact configured domain>` is accepted.
Ordinary addresses, including the existing Sam canary, cannot be delivered or
read. There is no external mail provider or forwarding fallback.

The service uses two distinct credentials, each 32–128 URL-safe characters:

- A private outbox-provider credential may `POST /deliver` with exactly
  `{deliveryId, email, code, issuedAt, expiresAt}`. The delivery ID is the
  existing durable outbox UUID; `code` is a six-digit string. `issuedAt` and
  `expiresAt` are the immutable challenge timestamps, not the retry's clock.
  Future, expired or unbounded source windows are refused. Acceptance returns202.
  Reusing an ID with different content or source times returns409. While its
  receipt is retained, the same ID and content retain the original `sentAt`.
- The CI-only read credential may `GET /inbox?email=...&after=...`. It returns404
  until the latest code for that exact fresh address follows the supplied aware
  timestamp, then `{email, code, sentAt}`. Both the source issue time and actual
  acceptance time must follow the cutoff. After a service restart, an old
  challenge therefore cannot satisfy a newer attempt. Neither credential can
  perform the other's operation.

Every response is `Cache-Control: no-store`. Access logging is disabled to
exclude addresses, request query strings, codes and credentials. Health returns
only `{status: "ok"}`. At most256 deliveries remain in memory until the earlier
of source expiry and five minutes after source issue;
idle cleanup removes expired records. A full inbox refuses a new delivery with503
and does not evict another valid code. Restarts lose the ephemeral codes and can
cause an acceptance attempt to fail; they cannot manufacture a successful run.

## Deployment preparation

`ops/founder-staging/compose.inbox.yml` is an operator-only fragment, excluded
from the existing runtime configuration. It has no published host port, durable
volume, database connection, provider key, Docker socket or privileged mode.
The external private network must already exist under the reviewed fixture
topology. Use only a verified image digest containing the tested source and a
protected environment file. Do not reuse the existing Resend key, Chat signer,
production keys or existing password-pilot credentials.

The later ingress change must expose only authenticated GET `/inbox` through an
explicit HTTPS staging host. `/deliver` remains private to the dedicated
staging outbox provider. Configure the repository's inbox URL and scoped read
secret only after the live service and delivery path are verified. Do not enable
the weekly schedule before the first actual manual acceptance run is reviewed.

## Remaining application wiring

The existing password-pilot API keeps signup and email codes disabled. The
fixture needs a separately reviewed staging API/frontend profile with its own
origin, cookies and credentials, preserving all eight existing accounts/roles.
Admission must enforce the reserved UUID-address namespace on the server.

Delivery must use the real `CommunityChatEmailCodeDelivery` outbox and ordinary
email-code verification. A dedicated worker must scope **both** expired
challenge cleanup and delivery leasing to the fixture profile's origin before
touching rows. The current unscoped worker cannot simply be enabled. Pass its
actual delivery UUID and immutable challenge creation/expiry times to this
provider, retain ordinary retry/lease behavior and
never fall back to Customer.io or another live provider. The normal draft-ready
and other four notification flags remain off.

The generation worker, isolated model provider, wildcard website and preview
fixture, source pins, network policy and actual frontend acceptance workflow
remain separate required work. No migration is added by the inbox component.

## Validation

Run `python -m unittest ops.founder-staging.test_inbox_fixture -v`. The tests
exercise real loopback HTTP plus scoped auth, freshness, retry identity,
concurrent retries, retention, capacity, payload bounds and redacted logs. Their
test clocks and synthetic data are unit controls, not elapsed rollout evidence
or live acceptance results. The module is also assigned to the normal database
free operational CI lane.
