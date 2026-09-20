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

Deploy compatible Django/Valley versions, update Chat's browser CSP to permit signed uploads to `https://storage.googleapis.com`, verify `COMMUNITY_CHAT_FRONTEND_URL` and allowed origins, and then enable the flag. Native source connection opens the system browser and returns to the web `/pulse` page; native users return to the app and source status refreshes. There is no new native deep-link callback. Existing providers requiring resource selection (for example Analytics properties) must already be configured; this increment does not add provider resource pickers.

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
