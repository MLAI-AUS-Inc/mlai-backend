# Internal campaign content check

The backend sends the rendered text/HTML MIME message to `POST /checkv2` with
`Settings-ID: crm_preflight` and `Flags: no_log`. The result is advisory at 3
points and blocks at 5. An unavailable or invalid result blocks a new Send.
The service has no SMTP capability and does not deliver email.

This Compose definition is a prepared deployment artifact. It is not started by
`deploy.sh`. Copy `.env.example` to the host's secret/config directory and select
a reviewed immutable `rspamd/rspamd:<tag>@sha256:<digest>` image before use.
Join only the backend's trusted `mlai-shared` Docker network. No port is
published. Controller and proxy workers are disabled. Message history and
learning are disabled, logs are errors only, and state directories are tmpfs.

Before an authorized staging deployment, validate the selected image with
`docker compose config`, `docker compose run --rm rspamd rspamadm configtest`
and `rspamadm configdump settings`. Verify its `curl` health probe returns
`pong`, then scan synthetic `.eml` fixtures with the same HTTP headers as the
backend. Confirm the effective profile excludes SPF, DKIM, DMARC and transport
reputation checks: the pre-send MIME has not yet passed through Resend. The
separate inbox check verifies actual receiver authentication and signed
unsubscribe headers. Review score fixtures after any image/rule update.

`RSPAMD_URL=http://rspamd:11333` is configured on the backend. A reachable
scanner and explicit seed configuration are release gates before enabling
`CRM_PREFLIGHT_ENABLED`. Seed records contain only a `CRM_SEED_*` environment
variable name. App passwords or externally refreshed OAuth2 access tokens live
in host secrets, never in the CRM database. `xoauth2` passes that token to IMAP;
the host secret manager must keep it current. Never configure a staff inbox as
a seed. Seed polling uses read-only selection and `BODY.PEEK`.

Real Gmail/Outlook/Yahoo placement, the selected image's runtime configuration,
and Resend's DKIM header coverage still require an explicitly authorized
staging rehearsal. Local fixtures do not establish those results.

Primary references: [Rspamd protocol](https://docs.rspamd.com/developers/protocol/),
[named settings](https://docs.rspamd.com/configuration/settings/), and
[official image and probes](https://docs.rspamd.com/getting-started/installation/).
