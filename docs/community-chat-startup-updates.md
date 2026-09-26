# Startup updates in MLAI Chat

Implemented locally on 20 September 2026. This is a gated integration, not evidence of deployment. It reuses the existing Founder Tools companies, reporting configuration, evidence snapshots, revisions and approvals. Valley remains an internal generation worker; Chat never receives service or provider credentials.

## Experience and scope

The React desktop/browser Pulse screen now opens **Startups**. `/pulse` remains the route so existing profile-panel links continue to work. Flutter is unchanged. The default journey is My startup → Add/select startup → Write or generate → Save and review → Approve privately or publish to Community updates. Monthly periods default to the last completed month in the startup's timezone. The setup form includes timezone and currency; website is optional. Separate staff review, weekly scheduling, notification delivery, and a mobile port are outside this increment.

Every request for startup-specific data must carry an explicit owned `company_id` (query) or `companyId` (JSON). Requests cannot fall back to the account's active startup. Account/community changes discard late client responses, and the backend independently checks ownership. The community reader is available to authenticated MLAI Chat accounts and reads approved community publications only.

## API

All paths are under `/api/v1/community-chat/startups/`. Except the signed browser handoff, they accept only `CommunityChatAccountAuthentication` and use the existing account-cookie origin enforcement or native opaque bearer session. Responses use `Cache-Control: private, no-store`.

| Path | Method | Contract |
| --- | --- | --- |
| `bootstrap/` | GET | Account public ID, relay URL, founder profile/companies and capabilities |
| `companies/` | GET/POST | Reuse company creation/upsert and organisation mapping |
| `active-company/` | POST | Select an explicitly owned company |
| `settings/` | GET/POST | Reporting timezone, currency and existing health configuration |
| `sources/` | GET | Scoped connector availability, status, account and warnings |
| `documents/` | GET | Company-owned documents and extraction status |
| `documents/session/`, `documents/complete/` | POST | Existing scoped signed-upload protocol |
| `connect/<provider>/` | POST | Five-minute signed, single-use Chat-session/company/provider ticket |
| `connect/browser/` | GET | Redeem ticket after checking session revocation/auth version; enter existing OAuth |
| `updates/` | GET/POST | Owner archive (50 per page, `offset`); save requires `saveMode: draft` |
| `updates/<id>/` | GET | Current owner revision and community preview; `version=published` reads approved revision |
| `updates/<id>/publish/` | POST | Requires `reviewed: true`, exact revision ID/hash and saved audience |
| `community/` | GET | Approved community revisions with matching hash/disclosure receipt; 50 per page |
| `generate/` | POST | Explicit company, targetMonth, selected inputSources, optional notes/document IDs |
| `runs/active/`, `runs/<id>/`, `runs/<id>/results/` | GET | Existing persisted run contract, restricted to this company's domain |
| `runs/<id>/cancel/` | POST | Existing scoped cancellation and cleanup |

The facade delegates domain behavior to the existing services rather than duplicating startup state. Generation uses the existing active-run deduplication; clients disable duplicate submission. It does not introduce a new durable idempotency-key protocol. Polling backs off from 2.5 to 15 seconds, stops on terminal status, and pauses in background. Completed drafts refresh the archive. Failed status reads offer retry.

## Connection tiles and lifecycle additions — 26 September 2026

The source catalog contains `gmail`, `stripe`, `humanitix`, `xero`, `bank_feed`, `notion`, `google_drive`, `slack`, `linear`, `google_analytics`, and `luma`. Existing fields are preserved; each source adds `connectMode` (`oauth` or `api_key`), `canConnect`, `canDisconnect`, and `usableForUpdates`. Chat discovers accessible Analytics properties automatically when a draft starts. The separate `connect/google/` handoff requests website/Search Console consent; it is not a Valley input. GitHub remains a separate publishing connection.

- `POST connect/<provider>/` returns `authorizationUrl` for OAuth. The browser redeems the one-use ticket and creates its own OAuth session, avoiding native API-cookie/browser-cookie mismatches. Luma/Humanitix instead accept `apiKey` through their existing scoped validators. The server chooses the return URL: `/my-startup/connections?company_id=<owned-id>&connected=<provider>` on the configured Chat origin. Native clients refresh when the founder returns to the app.
- `DELETE sources/<provider>/` requires the explicit company and disconnects only that company's accounts.
- `DELETE updates/<id>/` takes `companyId`, `revisionId`, and `revisionHash`. A changed revision or active generation returns 409. Deletion removes its community publication, revisions, approval receipts, and unreferenced snapshots. Shared evidence and connected accounts remain.
- `generate/` requires explicit `inputSources`. An empty selection with notes/documents becomes manual-only; an empty selection without evidence returns 400 instead of silently enabling Gmail. Existing independent-update identity and persisted step-progress contracts are unchanged.
- Draft saves accept `inputSources`, including an explicit empty list. Private memo selection metadata is separate from frozen evidence; changing toggles does not claim new data was fetched or rewrite past provenance.

Bootstrap advertises `capabilities.draftReadyPush: false`. Completed runs record one durable `startup_update_completion` receipt with identifiers, a timestamp, and `deliveryState: unavailable`. Repeated worker callbacks preserve it. This is an integration point for the existing attested push gateway, **not an enabled delivery queue**. No raw APNs tokens or alternative transport bypass the Chat privacy gate. Clients must not promise a push notification while this capability is false.

These additions create no schema changes. The local checks cover every signed OAuth provider, API-key delegation, cross-company disconnect isolation, exact-revision deletion, source selection and completion deduplication. They do not prove live provider consent, successful real-device OAuth, or push delivery. Live acceptance requires the founder's provider accounts and an approved environment.

## Evidence, editing and disclosure

- Generated content is private. Curation may include sensitive supported facts for founder assessment. Automated candidate selection is not human approval.
- Notes and extracted document excerpts are included in the versioned snapshot with content hashes; writer and verifier both receive that snapshot. Missing metrics remain unknown. Numeric zero is retained. Explicitly clearing an editable operating metric removes its frozen value. Imported financial, Analytics and Luma figures remain read-only under the existing founder-edit policy; their connectors own changes.
- A manual write or substantive human correction creates `validation.groundedness_status = founder_asserted`. It requires explicit human review. An audience-only or unchanged save preserves prior validation; it cannot erase failed/pending checks. Missing validation cannot publish.
- Approval verifies the exact current revision ID, content hash and audience. Later edits leave the prior approved version available until another revision is approved. Conflicts return 409. Draft saves through Chat do not award completion rewards.
- Community responses allow only narrative, selected metrics with display label/unit/quality, startup name, period and revision/publication identifiers. They exclude raw evidence, attachment metadata, storage paths, provider identifiers, financial charts and private analysis.
- Uploaded source documents remain private inputs. Their selection is editable. Full startup offboarding removes snapshots through the existing deletion service. Deleting an individual source attachment does not rewrite historical snapshots; frozen evidence is retained with the saved update.

## Rollout

`COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED` defaults to **false**. Disabled endpoints return 404 with an explanatory message. Keep it false until backend/database integration and configured-provider acceptance checks pass.

This increment adds no model changes or migration files. It depends on the already-present reporting schema, including `startup_updates.0022_reporting_evidence_revisions`, and the existing account-session schema. Confirm the deployed migration state before enabling. Never infer deployed state from local source or a historical plan.

Deploy compatible Django/Valley versions, update Chat's browser CSP to permit signed uploads to `https://storage.googleapis.com`, verify `COMMUNITY_CHAT_FRONTEND_URL` and allowed origins, and then enable the flag. Native source connection opens the browser and returns to the web Connections page; native users return to the app and source status refreshes. There is no new native deep-link callback. Chat automatically discovers provider resources when a draft starts; native and browser clients no longer require per-resource pickers.

The deployment's storage-CORS setup includes `https://chat.mlai.au`, `tauri://localhost` and `http://tauri.localhost` alongside the existing website origins. Keep these origins in the default policy because `deploy_postmigrate` reapplies it on every release. Signed uploads still require their scoped, expiring URLs; this does not make the bucket public.

Rollback by disabling the flag. Saved startups, drafts and approvals remain in Django. Switching off the flag disables access; it does not undo publications or delete source data.

## Validation and rollout acceptance

Checks on the isolated PR branches based on current main:

- 85 database-free backend tests covering the facade, review policy, evidence, source extraction, covers and progress contracts; system and model-drift checks pass.
- 61 database integration tests pass against the approved current-main inventory, including Chat journeys, exact revision approval, company isolation, independent update identity and reporting evidence.
- Valley full suite: 140 passed.
- Chat typecheck and 12 focused model/session tests pass. Five mocked browser journeys and five desktop journeys pass, including stale approval and resumed/cancelled/completed generation.
- The full Chat `just ci` run passed. Companion PRs: [Chat #188](https://github.com/MLAI-AUS-Inc/mlai-chat/pull/188) and [Valley #55](https://github.com/MLAI-AUS-Inc/valley-backend/pull/55).

The user-approved original 339-migration inventory was applied only to a fresh disposable SQLite test database: all 18 integration/revision tests passed. Current main subsequently added reporting identity/progress and other existing migrations. With user approval, the final 347-migration inventory was applied to another fresh disposable SQLite database: all 61 integration tests passed. Both databases were removed afterward. No migration file is created or modified by this feature.

The [current inventory](startup-update-pr-test-migrations-2026-09-20.json) SHA-256 is `171b4df656f530095e9222a352b946577be14657dbacf4b7aa79b48cada069f8`. The [original approved inventory](startup-update-test-migrations-2026-09-20.json) is retained as historical test evidence. The approved test command was:

```sh
.venv/bin/python scripts/test_startup_updates_database.py \
  --approved-inventory-sha256 171b4df656f530095e9222a352b946577be14657dbacf4b7aa79b48cada069f8
```

The runner validates migration file hashes, clears credentials and dotenv loading, blocks network access, and refuses any database outside its new temporary SQLite directory. Its suites cover Chat journeys, canonical revisions, independent update identity, reporting canaries, company scoping and progress-chart evidence.

These mocks and synthetic databases do not prove a live Django/Valley/provider round trip. Live OAuth, signed-storage CORS, production generation timing and runtime configuration require environment acceptance before enabling the flag. Xero's existing pre-generation sync can hold the HTTP request while refreshing stale data; check latency with the intended source before rollout. Merging follows the repositories' normal CI/deployment workflows; enabling the feature remains a separate runtime configuration action.


## Recent activity and persistent source defaults — 26 September 2026

`GET sources/` now adds `enabled`, `selectionMode: recent_activity`, and
`activityWindowDays: 30`. `enabled` represents inclusion in new drafts, separate
from account connection. `POST sources/<provider>/` with `enabled: true|false`
and an explicit owned company stores that company's default. It does not revoke
credentials, disconnect the account, delete data, or rewrite an existing draft.
Defaults live under the existing StartupProfile `progress_configuration` JSON,
keyed by company ID; no schema migration is needed. Clients use saved defaults
only when opening a new draft and continue sending the explicit draft allowlist.

Chat's independent-update generation defaults to a half-open narrative window
ending at the end of `updateDate` in the reporting timezone, capped at now, and
beginning 30 days earlier. Historical update dates retain their chosen endpoint.
Financial reports and stored metric observations keep their reporting-month
semantics; they are not relabeled as rolling totals.

- Slack, Analytics, and Linear catalogs are discovered automatically and pinned
  on the run. This does not overwrite legacy manual resource selections. Slack
  backfill receives the actual run bounds, and cached thread membership is based
  on in-window messages rather than a thread's latest reply. Extraction strips
  cached messages outside the range.
- Linear discovery includes the accessible catalog so historical projects are
  not lost to a current-day selection list. Worker bundles include only project,
  issue, or update activity in the chosen window and omit inactive projects.
- Notion pages must have a last-edited timestamp inside the activity window
  before their contents are hydrated. Gmail uses its existing period-bounded
  message and attachment evidence path.
- Luma event context uses the exact recent range rather than the prior month
  plus a two-week buffer. It continues to read cached connector data; this change
  does not introduce a fresh attendance sync during generation.
- Analytics uses the update day and preceding 29 days (its reports are
  day-granular), compared with the preceding 30 days.
- Google Drive remains connectable but is explicitly unavailable as an update
  input because this workflow has no Drive importer. The catalog says so instead
  of claiming its data will be included.

Catalog discovery is bounded to 20 pages and fails explicitly on a repeated or
unfinished cursor. Configuration failures return a useful 400 source error.
This keeps discovery scoped and avoids silently truncating a startup's resources.

Validation: 83 database-free tests cover facade/lifecycle contracts, persistent
company inclusion, recent date boundaries, resource paging, historical Slack
membership, cached-message exclusion, Luma and Analytics ranges, and the existing
date identity and source-evidence contracts. Cached Slack/Linear classification is reset once per run, only for captured resources with in-window activity, so a new rolling draft can reuse recent inputs without deleting prior evidence or content. No migrations or database-backed suites were run for this
change. Real provider consent, fresh-data latency, and Django/Valley round trips
remain release acceptance checks; these edits are not evidence of deployment.
