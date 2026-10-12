# Staging account federation

Work in progress; disabled by default. No production deployment is established.

## Cookie cutover

Deploy with `COMMUNITY_CHAT_HOST_ONLY_COOKIES=false`. Before enabling identity
federation, approve a cutover window and inspect real browser cookie behavior.
At the approved UTC cutover, enable host-only cookies for every web process and
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
