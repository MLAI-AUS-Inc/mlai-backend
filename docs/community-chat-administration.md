# Chat administration contract

`community_chat.Moderator` is a chat-only appointment with a unique user,
active flag and timestamps. It must not be represented as a PointsAdmin or a
Django staff account. Active linked PointsAdmin `admin`/`committee` accounts and
Django superusers retain Chat administration; other PointsAdmin roles do not.

Account endpoints use CommunityChatAccountAuthentication and require a verified
binding matching the session's user, public key and installation. A stale
session cannot inherit a different account's role after key reassignment.
All paths below are relative to `/api/v1/community-chat/`:

- `GET permissions/`: exact device key, configured relay URL, role and capability
  booleans. Requires a verified non-revoked device and active user for privileges.
- `POST member-roles/`: admin-only enrichment for up to 200 verified keys;
  returns only a public-key-to-role map.
- `POST moderators/<public_key>/`: body `{"enabled": true|false}`. Chat admins
  can appoint or disable a Moderator for the account owning the verified key.
  Self/administrator appointments are protected. Changes are in Django LogEntry.
- `POST account-bans/`: body `{"public_key": "<verified-key>", "reason": "optional"}`.
  Admin-only account-wide denial, preserving the account and canonical email.
  Self, staff and administrator targets are protected. Returns a ban record;
  HTTP 202 and `revocation_pending: true` mean account authentication is blocked
  but Chat device revocation is still retrying. HTTP 200 confirms that cleanup.
- `GET account-bans/?after=<id>`: admin-only active bans, including retained email,
  name, reason and revocation status. Returns at most 100 `bans` and a `next` ID
  (null when complete). Responses use `Cache-Control: no-store`.
- `POST account-bans/<id>/`: body `{"enabled": false}` lifts a ban after device
  revocation completes. It does not revive old sessions, imports or devices.
- `GET relay-roles/<public_key>/`: read-only service lookup returning only role,
  key and relay URL. Requires `Bearer COMMUNITY_CHAT_ROLE_SERVICE_TOKEN`, an
  independent secret of at least 32 bytes; defaults to disabled. Unknown,
  pending, revoked and inactive account/device bindings resolve to `member`.

The Chat relay pins its authority service to one tenant host, rechecks each
privileged action and fails closed on service errors. No role sync writes to the
relay membership table are required, so the existing member-only bootstrap and
device revocation boundary remains intact. Account permissions are never
accepted from caller-controlled profile metadata or client-supplied role tags.

Migration `community_chat.0010_moderator` was created with explicit approval and
applied to disposable SQLite test databases on 2026-09-09. The permission,
account session, device authentication, bootstrap API and throttle suites ran
67 tests: 64 passed and three existing row-locking tests were skipped on SQLite.
The schema drift check reported no pending changes. Tests used isolated settings
with environment-file loading and external networking disabled.

Apply this reviewed migration in production before enabling the role service.
Its scope is only the new Moderator table; it changes no existing admin records.
Production migration/application and deployment still require explicit approval
under AGENTS.md. No production migration or rollout was performed.

## Account-wide bans (locally validated, 2026-09-22)

The approved `core.0069_account_ban` creates a retained `AccountBan` record with
a protected account reference and unique canonical email. See the exact
[schema scope](../plans/account-ban-schema-2026-09-22.md) and
[migration](../core/migrations/0069_account_ban.py). With explicit user approval,
the migration was created and applied only to disposable local PostgreSQL
databases. Replay from `0068` preserved an existing synthetic account unchanged
and verified the case-insensitive email uniqueness constraint.

A ban sets `User.is_active=false`, increments `auth_version`, revokes Chat
access/refresh/bootstrap credentials and pending password reset challenges, and
pauses Slack imports. Django session hashes also include the authentication
version, so lifting a ban cannot revive old browser sessions. Magic-link
verification explicitly rejects banned accounts. The user save boundary
prevents stale profile/Slack updates from reactivating the account, replacing its
email or reducing the authentication version.

The existing device-revocation service cancels bound invitations, removes relay
memberships and fences private mirror registrations for every owned device.
Historical keys reassigned to another account are protected. The bridge worker's
maintenance turn retries pending revocations; failures preserve the ban and are
reported as pending rather than falsely confirming full revocation. Both ban and
unban actions use Django's administrator audit log. Django Admin also exposes
these service-backed actions; retained ban records cannot be deleted there.

Eight database-backed account-ban tests passed, covering every-device revocation,
retry after an adapter outage, preserved email, blocked case-variant signup,
magic-link denial, stale JWT/Chat credentials after ban and unban, the admin API,
member/moderator denial, and the database email constraint. Database-free
regressions also cover directory and ban guards.

The related PostgreSQL regression run passed all 112 tests across account bans,
Chat permissions/sessions, Slack mentions, authentication contracts, JWT sessions
and password APIs. Django system checks and migration drift checks passed. The
new ban and directory regression modules are included in the main CI test list.

`scripts/test_account_bans_disposable.py` replays the migration and runs these
tests using a fresh socket-only PostgreSQL cluster, synthetic settings, no `.env`
loading and no external network access. The runner also accepts specific related
test-module labels. It removes the cluster and its data after each run.
Production migration and deployment have not been performed.
