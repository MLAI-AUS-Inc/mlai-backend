# Architecture

## Role in the MLAI platform

`mlai-backend` is the shared Django service behind several MLAI product
surfaces. Browser applications and internal services call its HTTP APIs; worker
processes use the same Django models and configuration for asynchronous and
scheduled work.

```text
mlai-au --------------------+
roo ------------------------+--> Django API --> PostgreSQL
external webhooks ----------+        +--------> external providers
                                      |
                                      +--------> schedulers and workers
```

This is a logical view. Exact production networking and credentials belong in
the relevant deployment runbook, not in this document.

## Composition roots

- `manage.py`: Django command entry point
- `mlai/settings.py`: installed applications, middleware, databases, security,
  and integration configuration
- `mlai/urls.py`: public URL composition
- `Dockerfile` and `scripts/start-web.sh`: web container runtime
- `docker-compose.local.yml`: multi-service local development topology
- `docker-compose.yml`: deployed service topology

## Domain map

| Package | Responsibility |
| --- | --- |
| `core` | Authentication, users, shared permissions, and common API behavior |
| `organizations` | Organisation membership and organisation-owned state |
| `founder_tools`, `startup_updates`, `vibe_raising` | Founder-facing product data and workflows |
| `content_factory`, `content_analytics` | Content generation contracts, delivery, and analytics |
| `integrations` | OAuth and external connector surfaces |
| `community_chat` | Community chat identity, membership, and the live Slack bridge (dedicated bridge workers run in production) |
| `org_memory` | Governed organisational memory ingestion and retrieval |
| `roo` | Points and Roo-facing backend APIs |
| `jobs` | Job discovery and scheduled job operations |
| `hospital`, `esafety`, `generic_hackathons`, `hackathons` | Event and hackathon APIs |
| `data_access` | Reviewed access to shared data surfaces |
| `mlai_studio`, `victor_ai` | Product-specific application APIs |

The table is an ownership guide, not a complete module inventory. Consult each
package and its tests before changing a contract.

## Runtime processes

The deployed and full local topologies contain more than the web server. They
include schedulers and workers for discovery, analytics, organisational memory,
and password email. The repository also contains community-bridge workers from
the inactive Buzz/MLAI Chat experiment; code presence does not establish that
they are deployed. These processes share Django configuration and may share the
same database and cache when enabled.

Consequences for changes:

- A model or configuration change can affect web and worker processes.
- Background work must preserve idempotency and organisation scoping.
- Environment changes must identify every process that consumes the value.
- A healthy web process does not prove workers or scheduled jobs are healthy.

## Trust boundaries

- Browser credentials are accepted only from explicitly configured origins.
- Internal service credentials are distinct from end-user credentials.
- External webhooks must be authenticated, bounded, and replay-resistant.
- Organisation-memory providers require deployment enablement plus
  organisation-level governance approval.
- Historical Plane experiment configuration must not be treated as a current
  browser-origin or trust-boundary requirement.

Detailed security rules live beside each subsystem's implementation and
runbook. Preserve the existing fail-closed behavior when documentation and code
appear to disagree.

## Data and migrations

PostgreSQL is the production database. SQLite supports a subset of local and CI
checks. Redis/Valkey provides shared caching and coordination for features that
cannot safely use process-local state.

Database migrations require explicit approval for the exact migration. This
applies to manual commands and automatic startup behavior. See `README.md` and
`AGENTS.md` before starting a service or test harness.
