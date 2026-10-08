# Startup settings, research drafts, and branding

Implemented locally on 2026-10-05. This contract describes source behavior, not a deployed service. The logo correction on 2026-10-08 requires `founder_tools.0011_company_avatar_url_length`.

## Chat API boundary

`/api/v1/my-startup/` adds explicit profile/settings routes in `community_chat/my_startup_urls.py` ahead of the existing `founder_tools.my_startup.urls` facade. The established facade continues to handle its other registered operations. It accepts only revocable MLAI Chat account sessions, retains cookie-origin checks, applies Chat throttling, and returns `Cache-Control: private, no-store`. The added routes do not forward arbitrary legacy paths, worker operations, billing mutations, or administrative APIs. They reuse the established `MyStartupAuthentication` and response-link rewriting.

The current allowlist covers account identity/balance, founder profile/company/active-company, marketing bootstrap/settings, profile research and run status, existing run cancellation, logo, location/ABN lookups, GitHub account/repository operations, and the Article preferences notification channels and learned rules. Notification channels support list/create, verify/resend, delivery toggles, and removal; learned rules support list/retraction. Automation status is read-only here: changes must use the settings endpoint's prerequisite and price checks. The native `integrations/sources/status` alias uses the same selected-company source projection as the existing Chat Connections API. Other registered My startup routes retain the existing facade and authorization checks.

Company operations require an explicit owned `companyId`/`company_id`. Conflicting query/body aliases are rejected before effects. New-company requests use `createNew: true` and must omit an existing company scope. Profile saves include the ID in the body as well as any transport query. Organization access additionally requires the established organization owner. Account/profile/list routes do not require a selected company.

## Shared profile save

`POST founder-tools/companies/` is one atomic profile save. `name` is required; website/domain is optional. Omitted fields retain their saved value. Explicit empty strings or lists clear supported profile fields. Clients should submit only fields the user changed plus the required name and identity fields.

ABR registration verification runs only when an ABN or ACN is explicitly submitted. An unrelated name or profile edit preserves saved registration and identifiers even when ABR is unavailable.

Existing company/organization/startup-profile/configuration fields remain authoritative. The optional `founderProfiles: [{name, linkedinUrl}]` and `hasRevenue: "Yes" | "No" | ""` fields round-trip using the reserved `OrganizationContentConfig.pillar_strategy.startup_profile_details` namespace. Founder names are derived from explicitly supplied structured founder profiles. Timezone must be a valid IANA zone. Logo metadata uses the separate `startup_branding` namespace. Generated strategy merges cannot replace either namespace and worker/public strategy projections omit them.

Name-only startups use the existing synthetic organization mechanism. Their bootstrap returns saved descriptions, context, founder details, revenue, timezone, competitors, keywords, LinkedIn, and logo while website-dependent capabilities remain unavailable. The synthetic domain is not presented as a website in that bootstrap.

The Chat profile facade rejects GitHub repository and article preference fields. Repository selection belongs to Connections; `PUT vibe-marketing/settings/` owns article preferences. That settings path preserves sparse config writes and rolls back on rejected domain changes, malformed company LinkedIn URLs, unavailable daily prerequisites, or changed supplied point quotes. It honors `expectedCostPoints` when enabling daily automation and continues to use the existing eligibility checks.

## Reviewable company research

`POST vibe-marketing/autofill/` requires `draftOnly: true` on the Chat facade. Send `draftMode: true`, company name, website domain, explicit draft values, and either `companyId` or `createNew: true`.

Both `existingFields` flat values and nested `existingFields.profileFields` are accepted for allowlisted profile fields, including snake-case aliases. Explicit top-level values take precedence, including empty values. The normalized snapshot participates in the research fingerprint; nested payloads cannot replace company scope, domain, or persistence flags. Validation happens before a new research workspace is created.

For an existing startup, submitted draft values are sent to research without saving the company/profile or changing the active startup. A changed website returns `409 startup_research_domain_unsaved`; save that website change first. Research output remains in the existing run result shape, including `result.autofill.profileFields`, for client review and explicit later profile save.

For a new startup, research creates a minimal owned workspace with name/domain only and a private `researchDraft` marker. It is excluded from company lists and active-company fallback, and cannot be explicitly selected through the Chat switch endpoint until saved. `researchCompanyId` identifies it for polling and later save; clients must not require it to appear in the managed-company list. An explicit company save clears the marker, including a save without optional field changes.

Retries after a lost creation response recover the same owned, same-domain hidden workspace. Saved startups are never adopted through this recovery path. Matching active research fingerprints reuse the run; different snapshots return `409 startup_research_in_progress` with company/run IDs. New-workspace dispatches also carry a deterministic company-and-draft dispatch key. Profile row locking serializes workspace creation. An ordinary create/save can recover and promote the same hidden workspace after a lost research response.

For a new, unsaved startup whose website is edited after research, clients clear its provisional research ID and use a new-company request for the new website. Abandoned research workspaces remain hidden; automatic expiry/purge is not included here.

Company profile research does not debit Roo points in this API: the start response reports `costPoints: 0` and `charged: false`. The existing minimum-balance eligibility gate can still reject the request. This does not change prices for topic discovery, article generation, or scheduled automation.

`GET vibe-marketing/runs/{runId}/?view=status` retains run/company scope checks. The shared cancellation action does **not** support `startup_autofill`; clients must label a local polling stop as **Stop waiting**, discard late results, and not claim the server stopped research.

## Logo upload/removal

`POST vibe-marketing/company/avatar/` accepts multipart `avatar`; `DELETE` clears the logo and requires the explicit company ID in the query. Both return `company.avatarUrl` and `company.avatar_url` plus company identity. They do not imply a profile save.

Clients recommend a square image of at least 512 × 512 pixels and provide crop/zoom review. The server validates PNG/JPEG/WebP content, still images only, at most 10 MiB, 40 million pixels, and 16,384 pixels per side. It applies orientation, preserves alpha, and creates a transparent 512 × 512 PNG. Each upload uses a unique versioned storage path. Storage failure leaves the saved logo unchanged; removal clears the canonical pointer and returns clients to initials. Database failures roll back pointers; old or unreferenced storage objects are not deleted by this endpoint.

Firebase download URLs include the encoded object path and download token and exceed Django's default 200-character URL field limit. `VibeRaisingCompany.avatar_url` and its database column allow 2,048 characters after `0011_company_avatar_url_length`; preserve the full URL, including its token. The migration widens only this existing column and preserves nullability and existing values. Database failures during upload or removal return JSON HTTP 503 with `code: company_logo_save_failed` and retry guidance. Apply this migration through the approved backend release before expecting existing Chat clients to upload logos successfully. Creation and disposable local testing were explicitly approved; production application requires separate approval.

Only the established organization owner can set canonical branding. Own-company serialization falls back to its own legacy avatar until canonical metadata exists. An explicit empty canonical logo prevents an old avatar from reappearing. Pulse/community update DTOs read `startup.avatarUrl` from explicitly established organization metadata; they never choose an arbitrary founder's company logo. Historical updates use current startup branding.

## GitHub selection

`PUT vibe-marketing/github/repository/` accepts `{companyId, githubRepo}` with `owner/repository`, or an empty string to unlink. It verifies the founder's actual write access to a proposed repository before persisting. Changing it clears prior setup/scan caches and targets, preserves historical runs, and pauses daily generation/automation until reviewed again. It neither opens OAuth nor starts a scan. Response: `{githubRepo, repositoryChanged, requiresVerification}`.

`POST vibe-marketing/github/connect/` preserves founder-account reuse. When new authorization is needed, both `auth_url` and `authorizationUrl` point to the existing one-use Chat handoff at the backend origin under `/api/v1/community-chat/startups/connect/browser/`. The handoff validates the Chat session/company at callback and signs the return destination. `returnTo: "mobile" | "desktop-dev"` selects native return; browser uses the configured Chat frontend. Arbitrary return URLs are not used. Repository selection is a separate request.

## Verification and limits

Focused `unittest`/`SimpleTestCase` suites run through `scripts/test_without_database.py`, which removes environment credentials and rejects network/database access. Tests cover sparse/empty profile fields, optional-field round-trip, provisional promotion/retry, research non-persistence, explicit scope/authentication, repository validation/invalidation, canonical removal, alpha/limits, storage failure, and existing activation/editorial/Chat connection behavior.

The original profile/settings verification passed 198 isolated tests without migrations, database-backed tests, live provider calls or deployment. Client companion: [Chat PR #357](https://github.com/MLAI-AUS-Inc/mlai-chat/pull/357).

The 8 October logo correction separately passes 49 database/network-blocked tests and the model/migration dry-run. With explicit approval, `scripts/test_company_logo_postgres.py` creates a disposable local PostgreSQL cluster, reproduces the old 200-character failure, applies only `0011_company_avatar_url_length`, and verifies existing URLs, complete long URL persistence, rollback and removal. This narrow database regression does not prove organization locking, Firebase upload, GitHub authorization, worker execution or deployed routing. No production migration or deployment was performed.

Repository replacements use `bind_website` and unlinking disconnects the current `WebsiteConnection` generation. This preserves the existing authority checks and invalidates work for superseded connections. Shared GitHub authorization is retained. Settings responses are serialized after the atomic save commits, preserving worker reconciliation without a database lock.
