# Local Docker Against The Live MLAI Database

This setup runs `mlai-backend` and `content-factory` locally in Docker while pointing local `mlai-backend` at the real MLAI Postgres database. It is designed to avoid two common mistakes:

- pointing local traffic back at production `content-factory`
- reusing production Redis or public DB ports

The local flow assumes two runtime modes:

- `external_live_db`: the server-side `DATABASE_URL` already points at a real external Postgres host
- `tunneled_live_db`: the server-side `DATABASE_URL` points at `db:5432`, so the live database is still private to the server's Docker network and must be reached through an SSH tunnel

## What Was Verified In Repo

- [`mlai-backend/.env`](/Users/samdonegan/Documents/Code/mlai-backend/.env) uses SQLite locally today.
- [`mlai-backend/docker-compose.yml`](/Users/samdonegan/Documents/Code/mlai-backend/docker-compose.yml) still defines a local `db` service.
- [`mlai-backend/deploy.sh`](/Users/samdonegan/Documents/Code/mlai-backend/deploy.sh) excludes `.env`, so the real live DB config must be read from the remote server's runtime env, not this repo.
- The historical `postgres://...@db:5432/...` URLs found in git history are Docker-local defaults, not proof of an external production database host.

This Codex session could not inspect the remote server directly because SSH auth was unavailable from the sandboxed environment. The inspection step below is therefore part of the implementation, not precomputed output.

## Files Added

- [`docker-compose.local.yml`](/Users/samdonegan/Documents/Code/mlai-backend/docker-compose.local.yml)
- [`scripts/local-live-db/inspect-live-db.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/inspect-live-db.sh)
- [`scripts/local-live-db/write-local-env.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/write-local-env.sh)
- [`scripts/local-live-db/start-db-tunnel.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/start-db-tunnel.sh)
- [`scripts/local-live-db/stop-db-tunnel.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/stop-db-tunnel.sh)
- [`scripts/local-live-db/up-local-stack.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/up-local-stack.sh)
- [`scripts/local-live-db/down-local-stack.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/down-local-stack.sh)
- [`scripts/local-live-db/django.sh`](/Users/samdonegan/Documents/Code/mlai-backend/scripts/local-live-db/django.sh)
- [`../content-factory/docker-compose.local.yml`](/Users/samdonegan/Documents/Code/content-factory/docker-compose.local.yml)

The tracked examples live at:

- [`.env.local-docker.example`](/Users/samdonegan/Documents/Code/mlai-backend/.env.local-docker.example)
- [`.env.local-docker.example`](/Users/samdonegan/Documents/Code/content-factory/.env.local-docker.example)

The real generated files are ignored:

- `mlai-backend/.env.local-docker`
- `content-factory/.env.local-docker`
- `mlai-backend/.live-db-inspection.json`

## 1. Inspect The Live Server

Run this from [`mlai-backend`](/Users/samdonegan/Documents/Code/mlai-backend):

```bash
scripts/local-live-db/inspect-live-db.sh root@your-droplet /srv/mlai-backend .env docker-compose.yml
```

You can also provide the same values through env vars:

```bash
LIVE_APP_SSH=root@your-droplet \
LIVE_APP_DIR=/srv/mlai-backend \
LIVE_APP_ENV_FILE=.env \
LIVE_APP_COMPOSE_FILE=docker-compose.yml \
scripts/local-live-db/inspect-live-db.sh
```

That writes `mlai-backend/.live-db-inspection.json` and prints:

- the resolved runtime mode
- the redacted live `DATABASE_URL`
- `DATABASE_SSL_REQUIRE`
- Postgres roles if the live DB is the private Docker `db` service

If the inspection says `external_live_db`, you do not need a tunnel.

If it says `tunneled_live_db`, the script has confirmed the app still uses `db:5432` on the server and the tunnel workflow below is required.

## 2. Generate Local Env Files

By default the generator makes the local DB URL read-only by adding Postgres `default_transaction_read_only=on`.

```bash
scripts/local-live-db/write-local-env.sh
```

If you intentionally need write access:

```bash
DB_ACCESS_MODE=readwrite scripts/local-live-db/write-local-env.sh
```

This writes:

- `mlai-backend/.env.local-docker`
- `content-factory/.env.local-docker`

The generated overrides do three important things:

- point local `mlai-backend` at the inspected live DB target
- point local `mlai-backend` at local `content-factory` on `http://host.docker.internal:8001`
- point local `content-factory` at local `mlai-backend` on `http://host.docker.internal:8000` and local Redis on `redis://redis:6379/0`
- set `DEFAULT_FRONTEND_URL` and `MEDHACK_URL` to `http://localhost:3000` so local auth and Gmail OAuth redirect back to the real local frontend
- set `VALLEY_HARNESS_URL=http://valley-api:8080` so Gmail OAuth can auto-start the local Valley harness when the Valley stack is running on the shared Docker network
- set `VALLEY_HARNESS_API_KEY` to the same local internal service key as `ROO_API_KEY` and `INTERNAL_API_KEY` so local Valley auth matches `mlai-backend`

## 3. Start Or Stop The DB Tunnel

Only do this for `tunneled_live_db`.

Start:

```bash
scripts/local-live-db/start-db-tunnel.sh
```

Stop:

```bash
scripts/local-live-db/stop-db-tunnel.sh
```

What it does:

- creates a loopback-only bridge on the droplet at `127.0.0.1:15432`
- forwards that bridge through SSH to your local `localhost:15432`
- never exposes the live Postgres port publicly

If the inspection mode is `external_live_db`, the start script exits cleanly and tells you no tunnel is required.

## 4. Boot The Local Stack

Start:

```bash
scripts/local-live-db/up-local-stack.sh
```

Stop:

```bash
scripts/local-live-db/down-local-stack.sh
```

The local ports are:

- `mlai-backend`: `http://localhost:8000`
- `content-factory`: `http://localhost:8001`

The local compose files intentionally do not start `mlai-backend`'s local Postgres service, and they do not reuse production Redis.

## 5. Local Gmail OAuth

Local Gmail OAuth reuses the existing Google OAuth client configuration.

Before testing Gmail locally:

- make sure [`mlai-backend/.env`](/Users/samdonegan/Documents/Code/mlai-backend/.env) contains `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`
- keep `GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/integrations/callback/google` in `mlai-backend/.env.local-docker`
- keep `DEFAULT_FRONTEND_URL=http://localhost:3000` and `MEDHACK_URL=http://localhost:3000` in `mlai-backend/.env.local-docker`
- add `http://localhost:8000/integrations/callback/google` to the Google Cloud console's authorized redirect URIs for the current OAuth client
- restart the local `web` container after changing `.env.local-docker` so the running callback code and service keys match the repo
- if you want OAuth to auto-start Valley, start Valley on the shared Docker network instead of the standalone compose mode:

```bash
cd /Users/samdonegan/Documents/Code/valley-backend
docker network create mlai-local-shared >/dev/null 2>&1 || true
MLAI_LOCAL_API_KEY="$(grep '^INTERNAL_API_KEY=' /Users/samdonegan/Documents/Code/mlai-backend/.env.local-docker | cut -d= -f2-)" \
VALLEY_INTERNAL_API_KEY="$(grep '^VALLEY_HARNESS_API_KEY=' /Users/samdonegan/Documents/Code/mlai-backend/.env.local-docker | cut -d= -f2-)" \
docker compose -f docker-compose.local.yml -f docker-compose.mlai-local.yml up --build
```

Plain `docker-compose.local.yml` is standalone-only. It does not expose the `valley-api` hostname on `mlai-local-shared`, so `mlai-backend` cannot reach it at `http://valley-api:8080`.

Use the same browser session for both auth steps so the Django `sessionid` cookie issued by magic-link verification is present when you hit the Gmail connect route.

Recommended local flow:

1. start the stack with `scripts/local-live-db/up-local-stack.sh`
2. authenticate through `GET /api/v1/auth/verify-magic-link/` in the browser
3. visit `http://localhost:8000/integrations/connect/google`
4. confirm a [`GoogleConnection`](/Users/samdonegan/Documents/Code/mlai-backend/integrations/models.py) exists for your user
5. after the callback redirects back to Vibe Raising, trigger `email-draft/start` from the frontend and confirm Valley begins processing the run
6. if a prior local run is stuck, stop the Valley worker, dry-run the reset command, apply it, restart the worker, and then trigger a fresh draft run:

```bash
cd /Users/samdonegan/Documents/Code/valley-backend
docker compose -f docker-compose.local.yml -f docker-compose.mlai-local.yml stop worker

cd /Users/samdonegan/Documents/Code/mlai-backend
scripts/local-live-db/django.sh reset_startup_update_runs --domain mlai.au --older-than-minutes 5
scripts/local-live-db/django.sh reset_startup_update_runs --domain mlai.au --older-than-minutes 5 --apply

cd /Users/samdonegan/Documents/Code/valley-backend
docker compose -f docker-compose.local.yml -f docker-compose.mlai-local.yml start worker
```

Example check:

```bash
scripts/local-live-db/django.sh shell -c "from integrations.models import GoogleConnection; print(list(GoogleConnection.objects.values('user__email', 'google_email')))"
```

## 6. Safe Django Commands

Use the wrapper instead of calling `python manage.py` directly:

```bash
scripts/local-live-db/django.sh check
scripts/local-live-db/django.sh showmigrations
scripts/local-live-db/django.sh shell -c "from django.db import connection; print(connection.settings_dict['HOST'])"
```

The wrapper refuses commands that would mutate or bootstrap the live DB profile:

- `migrate`
- `makemigrations`
- `createsuperuser`
- `flush`
- `sqlflush`
- `loaddata`
- `dbshell`

This is a guardrail, not a substitute for judgment. The real safety default is that the generated DB URL is read-only unless you explicitly regenerate it with `DB_ACCESS_MODE=readwrite`.

## Expected Validation

After the setup:

- `mlai-backend` should start locally without its local `db` service
- `content-factory` should start locally with its own local Redis
- `mlai-backend` should call local `content-factory`, not the production host
- `content-factory` should call local `mlai-backend`, not `https://api.mlai.au`
- no public DB port should be opened on the droplet

## Notes

- Docker Desktop on macOS is the default assumption. The local compose files also add `host.docker.internal:host-gateway` so Linux can use the same hostname.
- If the remote deployment does not live in `/srv/mlai-backend`, pass the correct path to the inspection script.
- If the remote deployment uses a different compose file name, pass that too.
