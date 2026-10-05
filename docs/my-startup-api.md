# My startup account API

`/api/v1/my-startup/` exposes the reviewed marketing, company, connector, account and points operations to MLAI Chat account sessions. Existing JWT routes and Content Factory worker contracts remain unchanged. No database schema change is introduced.

The allowlist is `founder_tools/my_startup/registry.py`; its `urls.py` copies only those registered views. `community_chat/my_startup_urls.py` supplies explicit company-scoped facades before those aliases. The mixin preserves their permission and throttle behavior and adds authenticated Chat account requirements. Foreign explicit Authorization credentials cannot fall back to a Chat cookie. Cookie mutations retain the Chat session's exact-origin checks. Never add administrative or worker/service-key endpoints to the allowlist.

Company identity is explicit in frontend queries. The existing business views still enforce ownership. Private preview resources additionally encode the company in `/api/v1/my-startup/companies/<uuid>/vibe-marketing/runs/<run-id>/live-preview/…`, so iframe subresources keep their company scope without custom headers. Conflicting query scope is rejected. Responses and handoff material use private/no-store and no-referrer policies.

## Additional endpoints

### Request budgets

Authenticated My startup facades, reviewed legacy aliases and Chat startup
updates use separate per-account sliding-window budgets. Chat home, permissions
and Slack inventory requests cannot consume these budgets. Company and device
selection do not change the account key. Existing domain throttles remain
additive, and authentication, origin and company ownership checks still apply.

| Environment setting | Default | Operations |
| --- | --- | --- |
| `MY_STARTUP_BOOTSTRAP_RATE` | `120/minute` | Account (`auth/me`), balance, founder profile/bootstrap and marketing/startup bootstrap reads |
| `MY_STARTUP_READ_RATE` | `120/minute` | Other authenticated startup reads |
| `MY_STARTUP_POLL_RATE` | `120/minute` | Marketing run/research automation status and startup update active-run/status reads |
| `MY_STARTUP_WRITE_RATE` | `30/minute` | All authenticated startup mutations, including profile/bootstrap endpoints when mutated |

These limits are bounded across an account's companies and devices. A denied
request returns HTTP 429 with DRF's `Retry-After` header and a private/no-store
response. Clients should wait for that delay before retrying an idempotent read;
mutation retries must retain the operation's existing consent/idempotency rules.
The origin-checked, unauthenticated Roo token capture route keeps its existing
contract. Public startup update routes keep their existing public throttle.

The production incident observed on 5 October 2026 at approximately 23:32
Melbourne time was caused by startup loading sharing the `community_chat_home`
budget (`60/minute`) with Chat fan-out and run polling. A read-only inspection
of the current web container from 12:10 to 13:27 UTC (23:10 on 5 October to 00:27
on 6 October in Melbourne) counted 47 completed HTTP 429 responses: 20 My
startup requests, one Chat startup bootstrap request, 16 Slack user inventory
requests, six permission requests and four home requests. The 12:31 UTC burst
included nine My startup `auth/me` denials plus balance, bootstrap, profile and
run-status denials. At 12:30 UTC the log showed 15 successful account reads,
15 profile reads, seven balance reads, seven marketing bootstrap reads and
five run-status reads; these were all charged to the same account scope.
Request logs do not identify the account, so these traffic counts are aggregate,
not a claim that every request belonged to one session. Code and production
settings establish the shared bucket; the frontend's repeated page loads and
run polling explain how ordinary startup navigation could exhaust it. Frontend
inspection found run loaders issuing account, profile and status reads on each
poll. The intended 2.5-second cadence alone could charge 72 requests per minute
to the old shared bucket; a three-second cadence could charge 60 before other
Chat requests. Response-dependent effects could restart sooner still. Clients
must schedule the next poll after the previous request completes and respect
`Retry-After`; the separate backend budgets provide a bounded backstop rather
than replacing that requirement. A later
13:27–13:29 UTC retry returned HTTP 200 for startup reads after the window
expired. No health failure, database repair or migration was required for this
throttle fix. This record intentionally excludes raw logs and private identifiers.

Desktop Chat uses its protected native Chat account bearer session and omits cookies.
The exact Tauri origins `tauri://localhost` and `http://tauri.localhost` are allowed
through `DesktopAuthCorsMiddleware` for this namespace, including PUT catalogue
and settings saves. This does not enable native cookies or access to legacy JWT
routes. Deploy this CORS change before releasing the desktop My startup tab.
Provider OAuth, Roo account linking and embedded previews still use the browser
workspace because those operations depend on its cookies.

| Endpoint | Contract |
| --- | --- |
| `GET/POST /vibe-marketing/website-connection`, `POST /vibe-marketing/website-connection/{pause\|disconnect\|reconnect\|reset\|cleanup}` | Require the owned selected company. Lifecycle mutations require the reviewed website connection tuple and delegate to the canonical handler, including generation changes, durable cancellation, repository projection invalidation and separately approved cleanup. Reset retains company/editorial data and history; cleanup never receives approval from reset. Both trailing-slash forms are accepted. |
| `POST /connectors/<provider>/connect/` | Initiate Google Search Console, Google Analytics or Slack OAuth as the Chat user; require an owned company and a return URL under the configured Chat `/my-startup` origin. Provider callbacks stay at the existing backend URLs. |
| `DELETE /integrations/sources/connections/<id>?company_id=<uuid>` | Require an owned selected company, its organization and the user's Google Analytics or Slack connection before disconnecting. |
| `POST /points/me/purchases/` | Preserve existing packs/terms/checkout behavior and tag the purchase with the My startup surface. |
| `GET /points/purchases/<uuid>/`, `POST /points/purchases/<uuid>/checkout/` | Require purchase ownership. Checkout returns to Chat's `/my-startup/credits/<uuid>`. |
| `POST /roo-link/capture/` | Origin-checked token capture in a bounded, host-only HttpOnly cookie before sign-in. |
| `POST /roo-link/preview/`, `POST /roo-link/complete/` | Authenticated preview and explicit confirmation using only the captured cookie. Existing account-link rules remain authoritative. |
| `POST /handoff/redeem/` | Authenticated single-use retrieval of an old-origin research draft. Bound to the issuing account, owned company and any referenced research workflow. No dispatch or credit spending. |

The legacy JWT endpoint `POST /api/v1/founder-tools/my-startup-handoff/` creates the draft handoff. It accepts a bounded research bundle and known marketing destination. Cache tokens expire after ten minutes. Use a shared cache with atomic `add` in multi-worker deployments, not per-process local memory.

## Deployment switches

Configure `COMMUNITY_CHAT_FRONTEND_URL`, exact allowed account origins and CORS for the deployed Chat origin. Deploy backend support before exposing the Chat browser feature.

- `MY_STARTUP_DELIVERY_LINKS_ENABLED=false` keeps newly generated review and verification links on the existing site. Enable after the Chat pilot.
- `ROO_FOUNDER_LINK_CHAT_ENABLED=false` keeps new Roo account-link URLs on the existing site. Add Chat to Roo's `FOUNDER_TOOLS_LINK_ORIGINS` before enabling.

Disable those switches to roll back future link generation. Keep both route families available for already-issued links and in-flight jobs. No records are copied and no database rollback is needed.

## Verification

Run `python scripts/test_my_startup.py` with Python 3.11 and the backend dependencies. It suppresses dotenv loading, uses controlled test settings and fails any attempted database connection. This contract suite checks credential isolation, origins, path allowlisting, preview scoping, link rewriting, handoff identity, ownership calls and purchase/connector adapters.

Eight real-record ownership tests in `founder_tools/my_startup/test_database.py` passed on 19 September 2026 after explicit approval of the 346 existing migrations listed in `docs/my-startup-test-migrations.md`. The run checked that the available and applied migration sets exactly matched that inventory, used a fresh temporary SQLite database, disabled dotenv loading and external network access, and removed the database after testing. Django system checks reported no issues. The tests cover company reuse and isolation, explicit domainless startup selection, preview scope, purchases, connector ownership and research handoff.

The disposable SQLite ownership gate is complete. It does not establish deployed PostgreSQL behavior or real provider execution. Future migration execution still requires the approval specified in `AGENTS.md`; the isolated contract suite does not authorize it. The cross-repository staging checklist and frontend verification record live in mlai-chat's `docs/mlai/my-startup.md`.

On 20 September 2026 the user approved all 347 current migrations in disposable
SQLite/PostgreSQL test databases, including the upstream
`startup_updates.0024_startupprofile_progress_configuration`. A repeat local run
passed 69 tests: the complete Slack founder-link API class (including enabled
Chat link issuance) and the eight My startup ownership tests. It verified the
exact applied inventory, recorded zero external network attempts and destroyed
the database. The 20 isolated contracts and seven deployment runtime/configuration
tests also passed. The first full CI run exposed a missing `settings` import in
Roo link issuance; that import is fixed and the new destination regression
exercises the real API. Production already has migration 0024; this feature
still introduces no migration.
