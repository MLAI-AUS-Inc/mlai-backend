# MLAI Chat backend release and recovery runbook

This runbook covers the backend-controlled membership and Slack bridge services
for a staged MLAI Chat release. Infrastructure identifiers belong in the
private release record; credentials and message content never do.

## Candidate preflight

Run from the exact backend commit recorded in the release evidence:

```sh
python manage.py makemigrations --check --dry-run
python scripts/check_secrets.py
python manage.py check
APP_ENV=production DEBUG=false python manage.py check --deploy --fail-level WARNING
python manage.py test \
  core.tests.test_auth_contract \
  integrations.tests_community_bridge \
  integrations.tests_community_bridge_contracts \
  community_chat.tests \
  tests.test_runtime_hardening \
  tests.test_validate_prod_urls
```

Run the deployment check in a protected production-shaped environment with
its required Redis URL, connector keyring, and distinct service credentials.
The settings loader intentionally refuses to start when any of those are
missing; do not weaken that gate merely to make the check command pass.

Repeat the integration profile against production-matched Postgres, Redis, and
object storage. SQLite is useful for fast unit feedback but is not release
evidence for transaction locking, constraints, cache behavior, or media
authorization. Record only service versions, opaque environment ID, start/end
time, result, and log artifact.

## Security review map

| Boundary | Required evidence |
| --- | --- |
| Device identity | One-time challenge, expiry/replay rejection, proof-of-key control, role-escalation rejection, revocation, and throttling tests |
| NIP-98 | Exact URL/method/payload binding, bounded timestamp/nonce replay window, and invalid-signature tests |
| Browser session | Production CORS allowlist, CSRF trusted origin/cookie policy, secure cookies, TLS redirect, and no wildcard credentials |
| Email-code sign-in | Generic padded request response, encrypted durable outbox, six-digit one-use expiry, attempt limits, retry/dead-letter visibility, and no email/code/error detail in logs |
| Membership | Eligibility adapter failure closes access; invite/challenge values are absent from responses and logs |
| Media | Membership authorization on read/write, safe MIME/download headers, tenant isolation, and object-store denial tests |
| Slack ingress | Raw-body signature before parsing, five-minute replay window, 256 KiB limit, public mapped-channel-only normalization |
| Bridge egress | Dedicated signer, exact channel allowlist, origin ownership, deterministic delivery identity, bounded retry/dead letter |
| Secrets/logs | Independent rotation, encrypted/token secret-manager custody, no Slack token/private key/body/invite values in client or structured logs |

Any unresolved item blocks production approval. Record the reviewer and link to
evidence rather than copying sensitive values into the change record.

## Email-code cutover controls

MLAI Chat launches with `COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=true`,
`COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=false`, and
`COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=false`. The latter two switches are
temporary rollback controls for pre-launch clients, not supported member-facing
sign-in methods. `validate_prod_urls` rejects a production configuration that
enables either legacy path, disables email codes, or omits
`CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID`.

Before deploying the web/API process, provision independent values for
`COMMUNITY_CHAT_EMAIL_CODE_PEPPER` and
`COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET`, publish the Customer.io six-digit
code template, and start `run_email_code_worker` under the same immutable
release. Test request, delivery, expiry, resend, invalid-attempt lockout, scoped
session refresh, sign-out, and server-first device removal in staging. Never
copy a code, normalized email, token, or delivery ciphertext into release
evidence.

## Migration and backup exercise

1. Put the candidate in maintenance/drain mode so bridge workers stop claiming
   new rows. Record queue depths and let processing rows finish or return to a
   retryable state.
2. Create database and object-store backups with provider-native checksums.
   Record opaque backup IDs and retention class.
3. Restore both into an isolated production-shaped environment. Run integrity
   queries for users, device credentials, memberships, mappings, receipts,
   deliveries, links, and media objects; compare counts without exporting
   message bodies.
4. Apply migrations through the candidate schema and run the full integration
   profile. Record migration names, duration, locks, and result.
5. Exercise the documented rollback if migrations are reversible. If not,
   demonstrate a restore plus forward-fix path and make that the explicit
   production decision. Never improvise a destructive reverse migration during
   an incident.
6. Verify the previous supported browser/native clients against the candidate
   schema before declaring compatibility.

## Staging and bridge acceptance

Follow `mlai-chat-bridge-staging.md` with two humans, one linked and one unlinked
Slack identity. Verify duplicate/echo prevention and create/edit/delete/thread/
reaction behavior in both directions. After a fresh create and reaction in each
direction, run `verify_community_bridge_staging` and attach its content-free
JSON output.

Exercise failure deliberately: stop the adapter, observe a retry, exhaust one
synthetic delivery, investigate it, and use
`requeue_community_bridge_delivery <id> --confirm`. The same durable delivery
identity must complete once. Disable/re-enable the mapping and verify there is
no backfill of the disabled window.

## Deployment order

1. Apply additive database migrations.
2. Set a stable `PASSWORD_RESET_DELIVERY_SECRET`, deploy the backend image by
   immutable digest, and start `run_password_reset_email_worker`. Rotating this
   secret cancels pending encrypted links, so rotate only with the outbox empty
   or after intentionally invalidating those requests.
3. Deploy the bridge adapter by immutable digest and confirm its dedicated
   public key matches client release configuration.
4. Run health checks and one synthetic private adapter delivery.
5. Resume workers, then enable only the selected staging mapping.
6. Observe receipt/delivery rates, retry/dead counts, callback rejections,
   password-email failures, membership denials, auth throttles, and latency
   before expanding the cohort.

## Rollback triggers and procedure

Rollback on authentication bypass, role escalation, cross-tenant/media access,
secret or message-content leakage, duplicate/echo loop, sustained callback
signature failures, data corruption, or an agreed error/dead-letter threshold
breach.

1. Disable affected mappings and pause workers. Do not delete receipts,
   deliveries, or mappings during triage.
2. If the schema remains backward compatible, deploy the recorded prior backend
   and adapter digests and run smoke checks before resuming workers.
3. If the schema is incompatible, keep writes stopped and execute the rehearsed
   reverse migration or backup-restore/forward-fix decision from the release
   record.
4. Confirm membership, revocation, NIP-98, media access, bridge idempotency, and
   prior-client compatibility after recovery.
5. Record timestamps, owners, immutable targets, user impact, and follow-up.
   Preserve audit rows under the retention policy.

Production promotion requires named backend, security, operations, and
community owners plus an incident contact and monitored dashboard. Missing
evidence leaves mappings disabled and the release unpromoted.
