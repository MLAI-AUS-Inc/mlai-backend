# MLAI Operations domain transition

The existing operations dashboard is moving from `https://admin.mlai.au` to
`https://ops.mlai.au` so `admin.mlai.au` can become the Plane workspace.

Deploy and smoke-test the legacy admin Worker on `ops.mlai.au` **before**
deploying this backend change: once the backend changes, every new `app=admin`
magic link returns to `ops.mlai.au`. Keep the old admin Worker binding in place
through the rollback window.

## Frontend authentication contract

`admin` remains the legacy authentication **app-context identifier** even after
the hostname changes. The operations frontend uses these backend endpoints:

1. `POST /api/v1/auth/check-user/` with JSON `{"email": "...", "app": "admin"}`.
2. `POST /api/v1/auth/send-magic-link/` with JSON
   `{"email": "...", "app": "admin", "next": "/relative/path?optional=query"}`.
3. The emailed link always starts at `https://ops.mlai.au/verify-email`. Neither
   an `origin` nor a `redirect_uri` supplied by a caller changes that origin.
4. The verification page calls `GET /api/v1/auth/verify-magic-link/` with the
   emailed `token`, `app=admin`, and optional `next`. A successful response has
   `next_url=https://ops.mlai.au<next>` and sets the existing MLAI session/JWT
   cookies.
5. `POST /api/v1/auth/logout/` with credentials included authenticates using
   the `refresh_token` cookie, so it works even when `access_token` is expired.
   HTTP 200 returns
   `{"message":"Logged out successfully","refresh_revoked":true}`. A missing,
   invalid, or expired refresh returns HTTP 401 with
   `{"error":"Valid refresh credential required."}`. The frontend completes
   local logout for either 200 or 401 because both responses clear browser and
   Django session state. HTTP 503 means shared revocation storage could not
   confirm invalidation: access and Django session cookies are cleared, but the
   refresh cookie is deliberately preserved so the browser can retry revocation
   after Redis/Valkey recovers. HTTP 500 means server-side Django session
   invalidation failed. Failure responses must be surfaced and monitored because
   complete server-side invalidation is not proven.

The `next` value must begin with exactly one `/` and may contain a query string.
Absolute URLs, scheme-relative paths, fragments, backslashes, controls, and raw
or repeatedly encoded path separators are rejected with HTTP 400. Operations
access is limited to active users who are either in an active `PointsAdmin` role
accepted by `is_points_admin_user` or are Django superusers. Inactive users are
never reactivated by the operations verification path. Public account creation
with `app=admin` is rejected.

New refresh tokens carry a random session-family claim which survives rotation.
Logout revokes that whole family in the production shared Redis/Valkey cache,
so an older rotated copy cannot silently mint a new session. Because pre-change
tokens have no family claim, their logout writes a user-level cutoff applying
only to legacy tokens; that invalidates every older legacy rotation while newly
issued family tokens remain independently revocable. Django's server-side
session row is flushed, not merely hidden by deleting a browser cookie. JWT and
Django session cookies retain their required `.mlai.au` scope; the CSRF cookie
remains host-only to the API and is never broadened to Plane's parent-domain
scope.

Only `https://ops.mlai.au` is in the credentialed CORS and CSRF allowlists.
`https://admin.mlai.au` is deliberately untrusted before Plane is enabled:
otherwise Plane-origin JavaScript could call `https://api.mlai.au` with the
browser's parent-domain MLAI cookies without traversing the gateway. Lookalike
origins and HTTP variants are also not allowed.

Before enabling the operations login gate, audit historical admin links:

```sh
python manage.py link_points_admins_to_users --dry-run
python manage.py link_points_admins_to_users
```

Resolve every active full-admin row reported without a matching `User.slack_id`,
then verify each intended operator can complete a magic-link login. Unlinked
historical `PointsAdmin` rows intentionally do not grant browser access.

## Cutover and rollback invariant

Deploy this backend allowlist before or atomically with Plane mode, confirm
`https://ops.mlai.au` login/refresh/API/logout, and prove an
`https://admin.mlai.au` preflight gets no `Access-Control-Allow-Origin` header.
Rollback flips the gateway to the legacy Worker, whose server-side API calls do
not require browser CORS authority; operators continue at `ops.mlai.au`.
Re-adding `admin.mlai.au` is an emergency, separately reviewed configuration
change and must never overlap Plane mode.
Its edge gateway separately strips parent-domain MLAI authentication cookies.
