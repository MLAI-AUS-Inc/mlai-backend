# Staging account federation

## Independent staging pilot rollout

The isolated API runtime now uses a pinned PostgreSQL18/pgvector image and an
independent connector-encryption keyring. Existing private API configurations
must run `python3 ops/staging-chat/config.py upgrade-keyring --output
ops/staging-chat/.env` before validation; it adds only missing keyring fields,
preserves all other credentials, adds the default-off password-mode field and refuses partial/invalid keyrings. No key
is printed. The private database and cache have no host ports.

`ops/staging-chat/run.sh plan` inspects the full pending schema without applying
it. Fresh installation includes inherited backend and Django migrations, not
only the federation migration. On 14 October2026 the user approved the exact
364-migration inventory whose ordered checksum is
`9d72cf35d7c61de8bf1e969b504dc3178ca01f8d760f71c9be560d0fe97959ed`, solely for
the NEW `staging_account_api` database on staging Droplet170.64.215.174 and
disposable focused-test databases. Compare final PostgreSQL history, migration
file hashes and helper identities before application. Any changed inventory,
nonempty target or unexpected historical guard requires stopping. Production
is excluded. Preserve the schema on rollback; do not reverse irreversible
credential backfills.

Prepared source; disabled by default. Focused local browser/backend checks passed; live rollout remains pending. Broader platform testing is deferred at the user's request.

## Cookie cutover

Deploy with `COMMUNITY_CHAT_HOST_ONLY_COOKIES=false`. Before enabling identity
federation, approve a cutover window and inspect real browser cookie behavior.
During the approved window, pause browser session issuance/refresh, drain in-flight requests,
then record the UTC cutoff so every legacy browser session precedes it. Enable
host-only cookies for every web process and
run `python manage.py retire_legacy_chat_sessions --before <UTC-cutoff>` to
preview the count, then the same command with `--apply` after checking it.
The command revokes only pre-cutover web sessions; it does not remove accounts,
native sessions or relay membership. Readers reject the old cookie names after
cutover; sign-in and logout expire their old `.mlai.au` domain/path cookies.
Users may need to sign in again. Do not restore legacy cookie acceptance during
rollback. Staging uses independent names and its API's host-only cookies.

No remote CI is used for this work. Tests and rehearsals use local disposable
data only. Production migration and cookie changes require a separate approval.

## Identity issuer contract

Additive migration `community_chat.0016_staging_identity_federation` creates only
identity grants, local subject mappings and short-lived staging transactions.
Production application requires approval of this exact migration before enablement.
`COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=false` is the default and preserves
production relay/adapter routing. Enable only with host-only cookies, a separate
random confidential-client secret (at least 32 characters), and a named tester
allowlist of `core.User.community_chat_profile_id` UUIDs.

Issuer endpoints under `/api/v1/community-chat/federation/`: `requests/` accepts
only the registered staging callback, client ID and S256 challenge; `authorize/`
requires a current production browser account session, its exact origin and
approved community/tester status; `token/` consumes the hashed 60-second code
atomically; `eligibility/` can check only subjects already granted to this client.
Machine endpoints require the dedicated Bearer client secret, which never reaches
browser JavaScript. Only issuer/subject/display name/eligibility is returned.
No production relay device is created by granting identity. The production
account-only sign-in preceding approval may write normal login/bootstrap state.
Do not log request bodies or callback query strings at proxies/application servers.

## Isolated staging API

Use `DJANGO_SETTINGS_MODULE=mlai.settings_staging_chat`; this composition root
registers only the browser federation, own-account read, session, public profile,
permissions and key-proof membership endpoints. Production root URLs and workers
are excluded. It uses synthetic local accounts with unusable passwords, a copied
display name and opaque issuer/subject mapping. Staff/admin authority is never copied.
API access lasts five minutes; refresh rechecks issuer eligibility and fails closed.
An authoritative eligibility denial revokes the staging session. If two tabs
refresh the same token concurrently, the stale request receives 401 without
revoking the other tab's successfully rotated credentials.
No immediate removal of an already-connected relay is implied by token expiry.

`remove_staging_identity --subject <subject>` previews removal with this profile.
After removing the tester from the production issuer allowlist, apply this command
with `--apply` on staging. Local account/session access is disabled first. Each
pending invite and staging membership is revoked; adapter failure leaves the local
account disabled and reports incomplete membership cleanup for retry. The command
refuses the production settings profile. Staging logout affects only staging cookies.

The prepared `ops/staging-chat/compose.yml` has private PostgreSQL18/Redis7 and one
API process. The API joins `mlai-chat-staging_chat` only to reach the existing
membership adapter and Caddy; its database/cache remain on its own network. There
are no host-published ports or scheduler/email/integration workers. Initialize its
private `.env` with `config.py init --chat-env <staging-chat-env> --output <api-env>
--source-sha <backend-commit>`. Existing credentials are preserved by refusal to
initialize twice. Only the verified staging adapter token is copied.

Startup checks pending migrations and refuses to apply them automatically. Inspect
and approve the exact schema plan before applying migrations to a fresh staging API
DB. Build with the committed source revision, inspect its image revision/environment
labels, and record its immutable image ID before use. Do not print rendered Compose
configuration: it contains secrets. Use `docker compose config --quiet` for validation.
Keep federation disabled until the production cookie/issuer/client rollout is ready.

Local verification (no remote CI): use a disposable local PostgreSQL instance, synthetic credentials and two databases named `federation_local` and `federation_stage_local`. Set `DEBUG=true`, `DATABASE_URL` to the first local database and `FEDERATION_TEST_STAGING_DATABASE_URL` to the second. Run `python manage.py test community_chat.tests.test_federation_two_databases --settings=tests.federation_settings --keepdb --noinput`. Its transport calls the real API handlers in process; it does not claim a live HTTP/TLS deployment. The ordinary single-database runner reports this dedicated test as skipped. Browser tests separately exercise URL-scoped cookies and popup handoff against synthetic local fixtures. No production data is used.

The staging production-mode profile excludes unrelated Roo/Health Hack credential validation only when both `APP_ENV=chat-staging` and `DJANGO_SETTINGS_MODULE=mlai.settings_staging_chat` identify the restricted API. Other production deployments retain those checks. Federation remains disabled and the pilot tester list remains empty until later approval.

The API profile now has a guarded local deployment runner. From a committed
backend checkout: `ops/staging-chat/run.sh init --chat-env /path/to/staging-chat/.env`
creates independent owner-only credentials without exposing them; `check`
validates the profile; `build` records the source commit and immutable image ID.
`migrate` is an explicit, approval-dependent live schema operation and starts
only this profile's private data services before applying migrations. `start`
rejects changed/retagged images before changing services and runs migrations in
check-only mode. No worker or production integration process is started.
These live steps have not been run by this implementation.

Before any production cutover, inspect the actual production cookie behavior
without recording token values, pause browser session issuance/refresh and drain requests before choosing the
UTC cutoff, deploy the disabled issuer with the additive schema,
and execute the approved retirement command. Keep host-only cookie names and
security behavior enabled during rollback; disable federation instead of
restoring shared-domain cookie acceptance. New browser sessions after the cutoff
and native sessions are preserved by retirement. Production has not been touched.

The first tester list is deliberately deferred. Source flags are disabled and
the allowlist is empty. A local full monorepo gate was attempted in the chat
checkout and stopped while extracting the pinned Rust toolchain because the
Mac disk was full. Targeted Python/TypeScript/browser checks passed; that does
not establish Rust/mobile checks or a live production pilot. No GitHub workflow
was dispatched, no branch was pushed, and no PR was created.


Final local review found and fixed an overlapping-refresh revocation race. Its
regression failed before the fix and passed afterward; the complete affected
backend auth/device/federation suites passed 100 tests, and the separate-database
handler integration passed one test. These checks do not establish live TLS,
proxy, cookie cutover or successful real-relay messaging/upload behavior.


On 14 October 2026, local validation resumed after disk cleanup with about
14 GiB free. Main Rust and native Tauri formatting/clippy, desktop checks,
web/type checks and 12 analyzer tests passed. The full command was stopped
before disk exhaustion during first-time Flutter SDK installation; mobile
checks and later unit/build gates were not reached. The incomplete SDK and
its download were cleaned up. The user chose the completed focused browser/backend
checks for this browser-only pilot. Additional all-platform/mobile testing is
deferred; this does not represent a successful full `just ci` run. Live pilot
verification and rollout approval remain separate.
No remote CI, push or deployment was performed.

## Independent password pilot

This approved rollout uses a separate staging account database, never production
accounts. `COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED=false` is the default; enabling
it requires the exact staging settings/environment and federation disabled. Web
uses independent Secure/HttpOnly host-only cookies; iOS/Android use scoped bearer
credentials with `Origin: mlaichat://callback`. Desktop is excluded. CORS/CSRF
allow only the staging browser. Password authentication preserves normal key
proof, installation binding and rate limits; confirm grants only member access.

Provision with `provision_staging_pilot --manifest /private/accounts.json`
(preview), then add `--apply`. The owner-only JSON list contains objects with
`email`, `first_name`, `last_name`, and an absolute `password_file` referencing a
separate owner-only operator-owned regular file. The command creates ordinary
accounts, refuses existing accounts/reset/admin roles and creates no consent
evidence or outgoing email. Distribute each private password directly to its
intended tester, never through a repository or public chat.

`remove_staging_identity --email pilot@staging.mlai.au` previews removal of a
synthetic pilot; `--apply` disables its account and revokes account/bootstrap
sessions before revoking pending invites and relay memberships. Incomplete
adapter cleanup is reported and can be retried. Logout alone retains relay
membership. Rollback preserves volumes/schema, disables the password flag and
uses explicit pilot removal; do not reset existing accounts or reverse schema.

Update existing private configuration with `config.py upgrade-keyring`. After
source/schema/account readiness, `config.py enable-password --output <env>`
atomically activates only the password mode; `disable-password` turns it off.
Restart only the API afterward. Production identity federation stays disabled.

After an explicit removal rehearsal, `provision_staging_pilot --manifest
/private/accounts.json --reactivate` previews recovery of the same disabled
synthetic account; `--apply` reactivates it only after all devices are revoked.
Its supplied private password must verify against the unchanged existing hash.
Active, unmarked, privileged accounts or incomplete cleanup are refused. No
password reset or new consent is performed. This supports rollback rehearsal
without leaving the approved synthetic pilot unusable.
