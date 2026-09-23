# Backend API web handoff

## Verified ingress and slots

`api.mlai.au` uses Cloudflare HTTPS termination and reaches the backend origin
over HTTP on port 80. Before this handoff, Docker published the Django `web`
container directly on that port. The origin already runs host Nginx for
`analytics.mlai.au` on port 443; the API vhost in
[`ops/backend-api/nginx-api.conf.template`](../ops/backend-api/nginx-api.conf.template)
adds port 80 without changing the analytics listener.

After adoption, host Nginx forwards public API requests to `web` on
`127.0.0.1:8001`. During a code-only deployment, `web-candidate` starts on
`127.0.0.1:8002` from the newly built image. Both ports bind loopback only.
The deploy starts a missing database without recreating an existing one, and
recreates application services without starting their Compose dependencies.
This prevents routine `.env` changes, including `APP_RELEASE`, from restarting
Postgres during a code release. Database image or configuration changes need
a separately planned maintenance step.
The `web` service keeps its `mlai-backend-web` alias on the Docker shared
network for internal clients; the candidate has no shared-network alias.
Internal clients using that alias can still see a brief reconnect while `web`
is recreated. The public API route stays on a healthy instance.

Nginx preserves the API host, forwards Cloudflare's visitor scheme, and passes
the trusted client IP restored using Cloudflare's published ranges in the host
Nginx range file. An origin request without Cloudflare's scheme remains HTTP
and reaches Django's HTTPS redirect. Nginx overwrites incoming IP headers so
the Django throttles do not trust a caller-supplied first address. The API
vhost accepts the 250 MiB Vibe Raising video limit and preserves streaming
and upgrade headers.

## First adoption

The first release must have **no pending database migrations**. The deploy
script rejects first adoption before pausing any runtime writer if the
migration graph is pending. Before the port switch, the script checks current
`main`, verifies the existing direct web release, starts and verifies the
candidate, and tests a shadow API Nginx vhost while Docker still owns port 80.
It then stops the direct web container, installs the live vhost, reloads Nginx,
and requires the origin health endpoint to report the candidate release and
slot within about 15 seconds.
It recreates `web` on loopback, verifies it, and switches the proxy to `web`.

The Docker-to-Nginx port-owner change has a brief unavoidable service window.
If Nginx cannot take port 80, recovery removes the staged vhost, lets prior
Nginx workers drain, and starts the exact old direct-port container. Once the
candidate has been verified through Nginx, recovery keeps Nginx serving that
candidate while recreating the last-known-good `web` image on port 8001.

## Later code-only releases

The deploy script refuses to replace an existing candidate or to proceed when
the managed API vhost points anywhere other than the normal `web` slot. It
rechecks that the release SHA is current `main` before starting the candidate,
before changing the route, and before replacing the normal web container.

The candidate must answer `/healthz/ready` on loopback with the exact new
release. Nginx then reloads to the candidate. The script checks the active
route and waits for the previous Nginx workers to finish requests against the
old `web` before recreating it. After the new `web` answers the same release,
Nginx reloads back to `web`. The candidate remains available through all
release checks and until the previous Nginx workers drain. A failed code-only
release routes to the candidate while the recorded prior image and release
marker are restored on `web`, then routes back to the restored slot.

If Nginx workers have not drained after 120 seconds, the deploy leaves the
candidate running rather than cut off those requests. A later deployment
stops before replacing that candidate; an operator must inspect the remaining
connections and remove the stale container safely.

## Schema changes and interrupted releases

The proxy does not make schema migrations interruption-free. A specifically
approved migration still pauses all runtime writers, so API requests may fail
until the new runtime starts. If a migration partially applies, existing
forward-only recovery keeps writers stopped for audited repair. The code-only
image rollback is not used across an incomplete schema transition.

Shell traps cannot run after an abrupt runner or SSH kill. At the next deploy,
an API vhost still pointing at `candidate` or an existing candidate container
causes a fail-closed stop before either web process is replaced. Inspect the
origin health and both Compose containers and determine which image is safe
for the current schema before changing the route. The managed route helper is
[`ops/backend-api/switch-web-upstream.sh`](../ops/backend-api/switch-web-upstream.sh);
it tests Nginx configuration, replaces only the owned vhost, and restores the
previous file on syntax or reload failure.
