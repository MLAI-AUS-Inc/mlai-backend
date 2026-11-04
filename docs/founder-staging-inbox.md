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

## Scoped normal outbox component

The normal email outbox supports the exact future
`mlai.settings_staging_founder` profile. Its provider guard requires the fixed
founder staging frontend/API origins, reserved domain, separate write credential,
local email/account-creation capabilities and four notification flags off.
Every existing runtime profile retains its ordinary delivery behavior.

In fixture mode, expired-challenge cleanup and delivery leasing select only
that frontend origin and web client. The lease uses an `IN` subquery, retaining
the original delivery-row locks. Direct foreign IDs return without decrypting,
cancelling or sending. A direct legacy send is refused in fixture mode. The
provider carries the durable outbox UUID and original challenge timestamps,
disables redirects and environment proxies, and checks the accepted receipt ID
before marking the row sent. Encrypted-code cleanup and bounded retries remain.

PostgreSQL namespace/foreign-row/retry controls and real local HTTP delivery
pass alongside the complete existing email-code auth and delivery suites. The
test-only private DNS/port mapping does not prove hosted network readiness.

## Remaining runtime wiring

The existing password-pilot API keeps signup and email codes disabled. The
fixture still needs a separately reviewed staging API/frontend profile with its own
origin, cookies and credentials, preserving all ten existing accounts/roles.
Admission must enforce the reserved UUID-address namespace on the server.

The dedicated settings profile is source-only and has not been deployed. Start its dedicated
worker only in the validated fixture profile; the password-pilot worker cannot
simply be enabled. Complete server-side recipient/session admission and normal
email-code verification before activating signup. Draft-ready and the other
four notification flags remain off.

The generation worker, isolated model provider, wildcard website and preview
fixture, source pins, network policy and actual frontend acceptance workflow
remain separate required work. No migration is added by the inbox component.

## Proposed separate runtime boundary

`mlai.settings_staging_founder` calls the pure
`mlai.founder_staging_environment.checked_founder_environment` guard before
loading base settings or applications. The source profile is not selected by
any existing service. Importing it makes no connection, grants no access and
does not create its services. All dotenv entry points skip this exact profile.

The proposed routes are `founder-postgres:5432/founder_acceptance` with a
`founder_runtime` role, and `founder-redis:6379/0`. The guard refuses the old
pilot's database/cache, URL routing overrides, `PG*` variables, ambient provider
credentials (including supported secret-name aliases and Firebase's direct
environment consumers), the inbox reader token, startup migrations and notification flags.
Database/cache/signing/email-pepper/inbox-writer/adapter/delivery-encryption/
member-account credentials must be independent. Reserved mail uses only
`staging.founder.mlai.au`.

URL checks do not prove server isolation. Before any activation, prepare and
review the separate PostgreSQL service/network, restricted runtime role,
controlled migration-owner path, cache and relay/adapter. No old accounts,
aliases, sessions, tokens, roles or production data may be copied. A persistent
fresh database's full 382-migration application is a different actual plan from
the approved sixteen migrations on the existing pilot; obtain the specific
migration approval required by `AGENTS.md` before that operation. This guard
does not authorize provisioning, new credentials or network access.

The profile pins distinct API/frontend/relay origins, private adapter and
future Content Factory endpoints, delivery encryption and member-account
secrets, and disables native/device/password/federation modes. Its host-only
cookies never overwrite or clear old pilot/production cookies. Only the email
and account-creation capabilities are local; generation/website/provider
capabilities are still unavailable pending the separate fixture stack.

`FounderAdmissionMiddleware` admits explicitly named normal handlers and
methods. The real browser's anonymous email requests carry web identity in
their JSON, not an account key header. Admission validates the reserved email,
client/platform/installation and exact Origin before the normal view can
throttle or create challenges. Cookie calls require the local-key header;
refresh/logout refuse explicit native tokens, and signed enrollment alone may
use the normal short-lived bootstrap Bearer. Production JWT/legacy cookie
fallback is not used in this profile. Pending account/onboarding keeps its
ordinary consent, membership and user/session locking path; no user is made
eligible by this middleware.

Stored challenges and credentials are checked too. Request/verify prechecks
refuse foreign primary aliases, challenge origin/platform/client/installation
and recipient digests before the ordinary explicit throttle counters change.
Consumption rechecks the stored challenge under its original lock. Access
last-used, refresh rotation and logout revocation check the reserved primary,
client/platform/origin before writing, under their existing service boundaries.
Bootstrap checks the same primary before membership/enrollment operations.
Anonymous DRF precheck refusals use its ordinary403 response; middleware
transport refusals and explicit invalid-session responses use401.

The profile uses `ATOMIC_REQUESTS=False`. Normal services retain their explicit
user-to-credential locks. A request-wide transaction would keep the initial
session last-used write locked before onboarding takes the user lock, reversing
email verification's order. Liveness stays independent of database readiness.

Signed preview document/assets GET/HEAD has one exact normal proxy exemption
because iframe navigation has no cookie-binding headers. The existing view
must still validate its expiring run-bound signature and normal domain/member
authority in the separate database. No broad My startup or anonymous website
exemption is used. Normal copy/download handlers retain their current-export,
revision and ownership guards. The canonical download URL discrepancy still
needs its focused route fix and real export qualification. No actual fresh
signup, relay enrollment, preview, ZIP or provider result is proved here.

Run `python -m unittest mlai.test_founder_staging_environment -v` for the
database free startup refusal controls. They are also assigned to operational
CI. Ordinary email verification, pending onboarding and signed relay enrollment
remain the required user journey.

## Validation

Run `python -m unittest ops.founder-staging.test_inbox_fixture -v`. The tests
exercise real loopback HTTP plus scoped auth, freshness, retry identity,
concurrent retries, retention, capacity, payload bounds and redacted logs. Their
test clocks and synthetic data are unit controls, not elapsed rollout evidence
or live acceptance results. The module is also assigned to the normal database
free operational CI lane.
