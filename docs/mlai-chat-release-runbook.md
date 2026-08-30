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
  integrations.tests_community_bridge_avatar_backfill \
  integrations.tests_community_bridge_mention_backfill \
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

## Account sign-in controls

MLAI Chat launches with `COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=true`,
`COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=false`, and
`COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=true`. Email codes remain the human account
proof in the browser; the device-auth switch enables the state- and PKCE-bound
browser-to-desktop handoff and is not a password or alternate identity path.
`validate_prod_urls` rejects a production configuration that enables password
authentication, disables either required flow, omits the exact Tauri CORS
origins, or omits `CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID`.

Before deploying the web/API process, provision independent values for
`COMMUNITY_CHAT_EMAIL_CODE_PEPPER` and
`COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET`, publish the Customer.io six-digit
code template, and start `run_email_code_worker` under the same immutable
release. Test request, delivery, expiry, resend, invalid-attempt lockout, scoped
session refresh, explicit browser approval, missing/tampered/cross-request/
expired/replayed authorization-code and PKCE exchange, Tauri CORS preflight,
mobile `mlaichat://callback` enrollment, sign-out, and server-first device
removal in staging. Never copy a code, normalized email, state, verifier,
token, or delivery ciphertext into release evidence.

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

## One-off Slack avatar backfill

MLAI Chat began adding validated Slack avatar metadata to newly mirrored events
on 10 August 2026. Older Nostr events are immutable, so historical messages need
one metadata-enriching edit event before the browser can render the Slack
author's image.

The command is dry-run by default. It only considers undeleted Slack-to-Buzz
message links in enabled mappings with edit synchronisation enabled. It excludes
reaction receipt links, links created after the supplied cutover, and messages
that already received a successful bridge edit after that cutover. Reaction
receipt links use the internal `reaction:` source-message prefix and are not
rendered author-message rows, so they do not need avatar edits.

Run a dry-run first:

```sh
docker compose exec -T web python manage.py \
  backfill_community_bridge_slack_avatars \
  --before 2026-08-10T10:26:32Z \
  --limit 100
```

The JSON report contains `last_scanned_link_id` and `remaining_candidates`.
Use the cursor for controlled batches:

```sh
docker compose exec -T web python manage.py \
  backfill_community_bridge_slack_avatars \
  --before 2026-08-10T10:26:32Z \
  --after-link-id <last_scanned_link_id> \
  --limit 100 \
  --apply \
  --confirm-historical-edits
```

Each selected message gets a unique receipt, so repeating a batch cannot enqueue
the same backfill twice. The command reads each distinct Slack profile at most
once per pass, accepts only the existing approved Slack/Gravatar HTTPS hosts,
and does not persist raw Slack profile payloads. The bridge worker publishes the
pending edits asynchronously. Inspect worker logs and dead letters after each
batch before continuing:

```sh
docker compose logs --since 15m bridge-worker
docker compose exec -T web python manage.py inspect_community_bridge \
  --slack-channel-id <channel-id> \
  --recent-minutes 30
```

Operators without direct production SSH access can dispatch the restricted
`Backfill production community bridge Slack avatars` GitHub Actions workflow.
It applies the same validation, dry-run default, explicit apply confirmation,
batch cursor, and single-channel restriction.

## One-off Slack mention backfill

New Slack events retain their inline user/channel references until the bridge
worker resolves them with the scoped bot token. The worker caches `users.info`
and `conversations.info` results, emits plain `@Display Name` and `#channel-name`
text, and falls back to `@user`/`#channel` without exposing provider IDs when a
lookup is unavailable.

Messages mirrored before this behavior was deployed can be repaired while the
raw Slack receipt is still inside the retention window. The command is dry-run
by default, never guesses from generic placeholder text, and enqueues
idempotent edit deliveries only when retained Slack markup resolves to a
different body:

```sh
docker compose exec -T web python manage.py \
  backfill_community_bridge_slack_mentions \
  --limit 100
```

Apply in controlled batches, using `last_scanned_link_id` from each JSON report:

```sh
docker compose exec -T web python manage.py \
  backfill_community_bridge_slack_mentions \
  --after-link-id <last_scanned_link_id> \
  --limit 100 \
  --apply \
  --confirm-historical-edits
```

Use repeated `--slack-channel-id` arguments to restrict a run. Stop and inspect
bridge-worker retries/dead letters after every batch. A
`no_retained_slack_markup` result means the raw receipt has expired or the
original message did not contain a Slack user/channel reference; the command
does not rewrite those rows.

## Slack thread reconciliation

If Slack reply counts disagree with MLAI Chat or replies render as top-level
messages, use the dry-run-first `Reconcile production Slack threads` workflow.
Restrict the first run to one channel and no more than 25 roots. Review
`mismatches`, `wrong_parent`, `orphan_replies`, `wrong_broadcast`,
`duplicate_events`, `stale_links`, and `errors` before selecting apply mode.

Apply requires one explicit Slack channel and the historical-repair
confirmation. It processes each
root before its replies, waits for the bridge worker between mutations, and
stops on dead deliveries, timeouts, adapter lookup failures, or a configured
mismatch-rate guard. Use the returned `resume.latest` value for the next batch;
do not widen the channel set until the previous batch is clean and a second
dry-run reports no remaining mismatches.

Deploy the MLAI Chat adapter/client change before the backend change that sends
the new `source_created_at` and `broadcast` delivery fields. After both are
live, verify one normal reply and one Slack “also send to channel” reply in
Slack and MLAI Chat, then run:

```sh
docker compose exec -T web python manage.py inspect_community_bridge \
  --slack-channel-id <channel-id> \
  --recent-minutes 30
docker compose logs --since 15m --tail 200 bridge-worker
```

Rollback by disabling the affected mapping and pausing the worker. Never delete
receipts, message links, or relay events by hand during triage.

## Deployment order

1. Apply additive database migrations.
2. Provision independent `COMMUNITY_CHAT_EMAIL_CODE_PEPPER`,
   `COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET`, and
   `COMMUNITY_CHAT_ADAPTER_TOKEN` values, deploy the backend image by immutable
   digest, and start `run_email_code_worker`. Rotating the delivery secret
   cancels pending encrypted codes, so rotate only with the outbox empty or
   after intentionally invalidating those requests.
3. Deploy the bridge adapter by immutable digest and confirm its dedicated
   public key matches client release configuration.
4. Run health checks and one synthetic private adapter delivery.
5. Set the protected `COMMUNITY_BRIDGE_PRODUCTION_ENABLED=true` deployment
   variable only after the credential, topology, and mapping review is complete.
6. Resume workers, then enable only the selected reviewed mapping.
7. Observe receipt/delivery rates, retry/dead counts, callback rejections,
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
