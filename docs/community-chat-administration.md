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
