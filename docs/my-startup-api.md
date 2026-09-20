# My startup account API

`/api/v1/my-startup/` exposes the reviewed marketing, company, connector, account and points operations to MLAI Chat account sessions. Existing JWT routes and Content Factory worker contracts remain unchanged. No database schema change is introduced.

The allowlist is `founder_tools/my_startup/registry.py`; `urls.py` copies only those registered views. The mixin preserves their permission and throttle behavior and adds authenticated Chat account requirements. Foreign explicit Authorization credentials cannot fall back to a Chat cookie. Cookie mutations retain the Chat session's exact-origin checks. Never add administrative or worker/service-key endpoints to the allowlist.

Company identity is explicit in frontend queries. The existing business views still enforce ownership. Private preview resources additionally encode the company in `/api/v1/my-startup/companies/<uuid>/vibe-marketing/runs/<run-id>/live-preview/…`, so iframe subresources keep their company scope without custom headers. Conflicting query scope is rejected. Responses and handoff material use private/no-store and no-referrer policies.

## Additional endpoints

| Endpoint | Contract |
| --- | --- |
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
