# Developing against the real staging backend

Backend repository: MLAI-AUS-Inc/mlai-backend, branch `staging`. API: https://api-staging.mlai.au. Browser: https://chat-staging.mlai.au. These use independent synthetic accounts and data. Production passwords, JWTs, Slack IDs and provider keys do not belong here.

## Local development

Use Python 3.11 and the repository's pinned requirements. `ops/staging-chat/local-dev.sh up` starts loopback-only PG18/pgvector and Redis, generates a separate private `.scratch/staging-local.env`, checks existing schema, and starts the normal Django route tree on http://127.0.0.1:8000. `check` validates; `down` stops containers without deleting their data. It never sources `.env` or applies migrations.

An empty database deliberately refuses startup. Before using a new developer database, obtain approval for the exact current migration inventory and that disposable target; then apply only that approved inventory, or restore a verified synthetic-only schema snapshot whose migration history and hashes match. The earlier staging approval does **not** authorize everyone's local database. Never copy production data. Run only tests relevant to your change on a separate approved disposable schema; do not point Django's test runner at the hosted staging database.

Load the private local env using a data parser (not shell `source`) when running Django tools. With `DJANGO_SETTINGS_MODULE=mlai.settings_staging_local`, `APP_ENV=staging-local` and its local database credentials, run:

```sh
python manage.py staging_local_token --user developer@staging.mlai.au --create --output /private/path/local-token
```

`--create` explicitly creates an ordinary marked local fixture; subsequent calls omit it. Output must be a new file. The token expires in 15 minutes and cannot authenticate to hosted staging. Pass its content in `Authorization: Bearer <private token>` to local default-auth endpoints such as `/api/v1/auth/me/`. Existing domain service/admin/org permissions still apply. This real backend is the integration path; the optional frontend mock is a separate tool.

## Hosted credentials and requests

Use your assigned synthetic account in the staging browser. It uses host-only secure cookies and the exact staging Origin. Do not reuse production login details or copy browser cookies into another origin.

For API tools/native clients, POST `/api/v1/community-chat/auth/password/` with Origin `mlaichat://callback` and JSON containing `email`, private `password`, `client_id: mlai-chat-android`, and `device` with a valid generated 64-hex public key, UUID installation_id, platform `android`, name. Keep the same device identity across refreshes; the mobile app already implements this flow. Read the returned `session.access_token` privately, then pass it as Bearer with the same Origin. POST `/api/v1/community-chat/auth/session/refresh/` with the private refresh_token; POST `/api/v1/community-chat/auth/session/logout/` to revoke. Browser login uses `mlai-chat-web`, platform `web`, Origin https://chat-staging.mlai.au and cookies rather than tokens in its response.

GET `/api/v1/auth/me/`, GET `/api/v1/points/me/balance/` and versioned GET/PATCH `/api/v1/community-chat/account/` use normal domain contracts. PATCH requires the current `profile_version`; unknown fields and role changes are rejected. Service-key and admin endpoints still require their own authorization; no service keys go into client code.

## Add a backend function

1. Branch from backend `staging`. Add a normal view/service/URL in the owning installed domain app, or an ordinary include in `mlai/urls.py`. Preserve its existing permissions and organisation scoping.
2. Run focused local checks against the isolated approved test schema.
3. Review/merge or fast-forward into shared `staging` after checking all GitHub workflow triggers and PR associations. This rollout uses no remote CI; do not create a PR/workflow that launches it accidentally.
4. The Droplet timer checks every two minutes and builds/deploys compatible commits. Call the normal path on https://api-staging.mlai.au. There is no staging endpoint whitelist to update.

New apps/dependencies, migrations, authentication/provider policy, operational files and external sandbox credentials require a separate reviewed manual rollout. Ordinary domain functions/URL additions do not. A failed deployment retains the last healthy version; ask the operator for controller status rather than changing infrastructure from application code.

Unavailable integrations return HTTP503 with `code: staging_integration_unavailable` and `capability`. Staging has no real email, payment charge, Slack/calendar reservation or outbound provider access. Synthetic purchases explicitly exercise local persisted outcomes. A reachable normal endpoint still follows its domain roles and may depend on an unavailable provider; availability is not an admin grant.
