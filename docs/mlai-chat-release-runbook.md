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

## Member-authorized recovery

The avatar, mention and direct thread replay/backfill operators and their manual
workflows have been retired. Existing delivery receipts, immutable requests and
poster ownership remain; do not delete or relabel queued work during cleanup.
Current metadata enrichment uses member authority with scoped caches.

Preview repair without Slack calls or database writes:

```sh
docker compose exec -T web python manage.py message_sync_repair \
  --workspace-id T0123456789 --channel-id C0123456789 \
  --thread-ts 1785550000.000100
```

After reviewing the exact source and job plan, add `--apply` to schedule normal
worker recovery. Existing leases, pagination checkpoints, cooldowns and archive
windows are retained. The worker revalidates current member, consent, OAuth,
source and audience authority before any provider operation. This does not
recreate orphaned historical events or rewrite old cosmetic metadata. See
[bounded repair](slack-member-repair.md) and
[permanent operations](slack-member-permanent-operations.md).

Keep member behavior enabled through compatible image rollback. Capture and
verify the coordinated backend/relay/adapter backup before withdrawing old
repair tools; restore data only for an actual data-loss recovery. Never delete
receipts, links or relay events by hand during triage.

## Deployment order

1. Deploy the membership adapter that advertises
   `generation_cas_v2` from authenticated `GET /v1/capabilities` and implements
   the v2 intent/CAS-mint endpoints. During this compatibility window it may
   continue advertising and serving `legacy_v1` for the previous backend only.
   Prove that a key delete between intent capture and mint rejects the stale
   mint and leaves no member or bound invite before deploying the backend.
2. Apply additive database migrations.
3. Provision independent `COMMUNITY_CHAT_EMAIL_CODE_PEPPER`,
   `COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET`, and
   `COMMUNITY_CHAT_ADAPTER_TOKEN` values, deploy the backend image by immutable
   digest, and start `run_email_code_worker`. Rotating the delivery secret
   cancels pending encrypted codes, so rotate only with the outbox empty or
   after intentionally invalidating those requests.
   The new backend must fail closed unless `generation_cas_v2` is advertised
   and must never fall back to the legacy invite POST. After its production
   membership smoke is green, deploy a membership-adapter follow-up that
   removes `legacy_v1`.
4. Deploy the bridge adapter by immutable digest and confirm its dedicated
   public key matches client release configuration.
5. Run health checks and one synthetic private adapter delivery.
6. Set the protected `COMMUNITY_BRIDGE_PRODUCTION_ENABLED=true` deployment
   variable only after the credential, topology, and mapping review is complete.
7. Resume workers, then enable only the selected reviewed mapping.
8. Observe receipt/delivery rates, retry/dead counts, callback rejections,
   password-email failures, membership denials, auth throttles, and latency
   before expanding the cohort.

### Owner Slack conversation inventory activation

Set the protected deployment variable `SLACK_OWNER_INVENTORY_ENABLED=true` only
when `MESSAGE_SYNC_ENABLED=true` for the same release. The deployment stages
inventory as `false` on the host, applies the approved inventory migration with
the rest of the Django migration graph, and checks the graph before it writes
the requested inventory value and starts the new runtime. It verifies the web
container received that value and, when enabled, the bridge worker did too. A
failed deployment stages inventory back to
`false` during recovery. Leave the variable `false` to disable the endpoint;
this switch does not change a member's Slack history consent.

After a member separately consents to the owner directory, the bridge worker
lists private conversation metadata in bounded pages independently of message
history import. The directory can therefore show old DMs while their messages
remain outside the selected history window. Read-state snapshots use their own
worker and may still be marked unknown or stale while Slack calls are pending.

### Reviewed `#tech_volunteers` public bridge mapping

After the backend deployment and migration check, dispatch
`community-bridge-tech-volunteers.yml` with `mode=stage`. It writes only the
reviewed `T05N9C1QSJC:C0BS0J2Q3M1` to
`da406415-80c3-5a53-82a9-3d200597b856` mapping, disabled. Use
`mode=inspect` to confirm the fixed destination and disabled state. Deploy the
MLAI Chat relay room and allowlist, then verify the bridge signer is a member.
Only then dispatch `mode=enable` with `confirm_relay_ready=true`. The enable
step also checks that the mapping was staged and the bridge bot belongs to the
reviewed local public Slack channel. Inspect again after enabling. The
workflow accepts no channel IDs or shell commands as inputs. It does not
create or map private Slack channels.

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

## Dependency and unrepresentable-delivery cleanup

Owner approval is required; never run repairs during a deploy. Operators use
`ssh mlai-droplet` and the container's `python manage.py` commands. Start with
`resolve_unrepresentable_slack_dm_deliveries --stuck` and
`resolve_community_bridge_dependencies --include-dead-reaction-adds`. Review
content-free JSON verdicts, then repeat the exact selection with
`--apply --expected-plan SHA256`. Changed plans need another review.
Reaction requeue additionally requires `--requeue-dead-reaction-adds
--confirm-adapter-reaction-fix`, after the adapter parity release is confirmed.
Flattening requires separate owner approval and `--flatten-replies
--confirm-flatten`. Only then consider enabling the GitHub variable
`COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED`; its default is false.
The dependency-review dispatch workflow is read-only and provides no apply mode.
