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

The Chat cancellation facade passes the run ID and `cancel` action by name to
the shared run controller, so its recovery path remains available after the
repository source changes. Authentication, explicit company ownership, run
ownership and external publication evidence checks still apply. Cancellation
does not grant setup, preview or publishing authority for the changed source.

## Logo upload/removal

`POST vibe-marketing/company/avatar/` accepts multipart `avatar`; `DELETE` clears the logo and requires the explicit company ID in the query. Both return `company.avatarUrl` and `company.avatar_url` plus company identity. They do not imply a profile save.

Clients recommend a square image of at least 512 × 512 pixels and provide crop/zoom review. The server validates PNG/JPEG/WebP content, still images only, at most 10 MiB, 40 million pixels, and 16,384 pixels per side. It applies orientation, preserves alpha, and creates a transparent 512 × 512 PNG. Each upload uses a unique versioned storage path. Storage failure leaves the saved logo unchanged; removal clears the canonical pointer and returns clients to initials. Database failures roll back pointers; old or unreferenced storage objects are not deleted by this endpoint.

Firebase download URLs include the encoded object path and download token and exceed Django's default 200-character URL field limit. `VibeRaisingCompany.avatar_url` and its database column allow 2,048 characters after `0011_company_avatar_url_length`; preserve the full URL, including its token. The migration widens only this existing column and preserves nullability and existing values. Database failures during upload or removal return JSON HTTP 503 with `code: company_logo_save_failed` and retry guidance. Apply this migration through the approved backend release before expecting existing Chat clients to upload logos successfully. Creation and disposable local testing were explicitly approved; production application requires separate approval.

Only the established organization owner can set canonical branding. Own-company serialization falls back to its own legacy avatar until canonical metadata exists. An explicit empty canonical logo prevents an old avatar from reappearing. Pulse/community update DTOs read `startup.avatarUrl` from explicitly established organization metadata; they never choose an arbitrary founder's company logo. Historical updates use current startup branding.

## GitHub selection

`PUT vibe-marketing/github/repository/` accepts `{companyId, githubRepo}` with `owner/repository`, or an empty string to unlink. It verifies the founder's actual write access to a proposed repository before persisting. Changing it clears prior setup/scan caches and targets, preserves historical runs, and pauses daily generation/automation until reviewed again. It neither opens OAuth nor starts a scan. Response: `{githubRepo, repositoryChanged, requiresVerification}`.

`POST vibe-marketing/github/connect/` preserves founder-account reuse. When new authorization is needed, both `auth_url` and `authorizationUrl` point to the existing one-use Chat handoff at the backend origin under `/api/v1/community-chat/startups/connect/browser/`. The handoff validates the Chat session/company at callback and signs the return destination. `returnTo: "mobile" | "desktop-dev"` selects native return; browser uses the configured Chat frontend. Arbitrary return URLs are not used. Repository selection is a separate request.

## Not-on-GitHub exit (A21)

`POST founder-tools/website-lead/` (Chat alias `/api/v1/my-startup/founder-tools/website-lead/`, `my_startup_write` budget) records that the founder's website code is not on GitHub. It needs no startup. It rides the GitHub-first gate `ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE`: with the gate off it returns 404 `{reason: "not_enabled"}` and records nothing, so a client treats 404 as "nothing to record".

Body: `{reason?, domain?, hostClass?, notifyWhenChanged?}`. `reason` is one of `not_on_github` (default), `other_git_host`, `not_in_git`, `someone_else_holds_code`, `no_github_account`; anything else is 400 `{reason: "invalid_lead"}`. `domain` is optional text, normalised to a bare hostname or dropped; it is never fetched. `hostClass` is a short lowercase id, otherwise `unknown`. `notifyWhenChanged` must be a boolean; send `true` from "Tell me when this changes". Once set it stays set.

Response: 201 for the first record of that reason and domain, 200 afterwards, with `{lead: {reason, domain, hostClass, notifyWhenChanged, remindAt, createdAt}, recorded, message}`. `message` is the one honest sentence to show ("MLAI can only set up articles for websites whose code is on GitHub today.", plus "Thanks. We'll email you when that changes." once notify is on). `recorded: false` means the lead could not be stored for this account (unverified email, or the email's CRM contact belongs to another account); the funnel event is still emitted, once per account, reason and domain. The lead key is system-owned in the CRM (`RESERVED_ATTRIBUTE_KEYS`), so CRM edits and CSV imports never overwrite it.

Storage: the lead row is an entry under `attributes.founder_website_leads` on the account's CRM `Contact` (no migration). Writing it never changes consent, status or owner. The first record emits one `not_on_github` founder journey event, and `journey_funnel` counts the stage from the stored rows. Nothing sends reminders yet (E3b).

## Website preflight (E1), one name rule (E2), website required (E4), duplicates (E5)

`POST founder-tools/website-preflight/` (Chat alias `/api/v1/my-startup/founder-tools/website-preflight/`; `my_startup_write` plus its own per-account `founder_website_preflight` budget) checks an address before a startup exists. It rides `MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED` (code default off): off, it returns 404 `{reason: "not_enabled"}` and fetches nothing, and a client keeps today's website step. Body `{domain}`; anything that is not a public hostname (an IP, `localhost`, `.invalid`, `.local`, `.test`) is 400 `{reason: "invalid_website"}` with no lookup.

Response: `{normalised, reachable: yes|no|unknown, check: {outcome, httpStatus, redirectHost}, hostClass: git_host|ai_builder|no_export|unknown, platform, guidanceId, builderName, deploymentUrl, contentSystem, suggestion, suggestedName, displayName, existing: {state: none} | {state: yours, companyId, name, match, message}, notes: [{code, message}], cached}`. `reachable` is `no` only for NXDOMAIN or a refused connection; a 403, 429 or 5xx homepage, a timeout or a TLS fault is `unknown` and never blocks. `check.outcome` always says what happened (`ok`, `redirect`, `http_status`, `nxdomain`, `dns_error`, `not_public`, `refused`, `timeout`, `tls_invalid`, `tls_error`, `connect_error`). `suggestion` is a close spelling, offered only when the address does not resolve and the suggestion does. `guidanceId` and `hostClass` use Chat's builder-guidance ids (`founder_tools/website_hosts.py` holds the table; Chat mirrors it). `deploymentUrl` is true on a hosting platform's own address (`*.workers.dev`, `*.up.railway.app`, ...): decision D10 accepts it for setup, and the note asks for the public address before the first article goes live. `existing` reads the caller's own profile only.

The homepage probe (`website_live_fetch.probe_homepage`) has its own DNS step and returns a status instead of raising; it keeps the verification fetch's guards (global addresses only on every hop, the connection pinned to a validated address with the original TLS name, never cleartext, only apex or www hops followed; an off-site redirect is reported, never fetched). `fetch_live_route` is unchanged. Results are cached per domain for 15 minutes (2 minutes for an `unknown` answer).

E2: the startup's name comes from one server rule, `website_hosts.suggested_company_name`: the homepage's `og:site_name` or title (never a template title such as "Vite + React"), else `derive_company_name`, which strips platform suffixes, account labels and random or deployment suffixes on a hosting platform (`auditmind-prod.up.railway.app` is "Auditmind"; a Cloudflare-generated `spring-surf-fc2e.<account>.workers.dev` gives no name rather than "Workers") and skips generic leading labels on the founder's own domain (`platform.tailoredagents.ai` is "Tailoredagents", `examon.jtutes.com` "Examon"). Clients show `suggestedName`; the one-field create saves it when no name is given.

E4: while the GitHub-first gate `ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE` is on, the My Startup one-field create (`createNew` + `firstProfile`) needs a website: none, or a `.invalid` placeholder, is 400 `{reason: "website_required"}`. With the preflight also on, the create re-runs it (cached): an address that does not exist is 400 `{reason: "website_not_found", suggestion}` unless the body says `useAnyway: true`. Nothing else changes: `ensure_company_organization`, the legacy company create and every fundraising and connector create still make or bind domainless companies.

E5: the one-field create checks the caller's own profile for the same site (a leading generic subdomain stripped; two sites on one hosting platform stay two sites) or the same name, and answers 409 `{code: "company_already_added", reason: "already_added", companyId, name, match: domain|name, canAddAnyway}` ("You already added this. Open it to carry on."). For a name-only match `notDuplicate: true` creates anyway. Another account's startup is never named: the exact-domain claim (`company_domain_claimed`) stays the only cross-account answer. Look-alikes across accounts (same site or same name) are listed for staff only by the read-only `python manage.py founder_duplicate_lookalikes`.

## Verification and limits

Focused `unittest`/`SimpleTestCase` suites run through `scripts/test_without_database.py`, which removes environment credentials and rejects network/database access. Tests cover sparse/empty profile fields, optional-field round-trip, provisional promotion/retry, research non-persistence, explicit scope/authentication, repository validation/invalidation, canonical removal, alpha/limits, storage failure, and existing activation/editorial/Chat connection behavior.

The original profile/settings verification passed 198 isolated tests without migrations, database-backed tests, live provider calls or deployment. Client companion: [Chat PR #357](https://github.com/MLAI-AUS-Inc/mlai-chat/pull/357).

The 8 October logo correction separately passes 49 database/network-blocked tests and the model/migration dry-run. With explicit approval, `scripts/test_company_logo_postgres.py` creates a disposable local PostgreSQL cluster, reproduces the old 200-character failure, applies only `0011_company_avatar_url_length`, and verifies existing URLs, complete long URL persistence, rollback and removal. This narrow database regression does not prove organization locking, Firebase upload, GitHub authorization, worker execution or deployed routing. No production migration or deployment was performed.

Repository replacements use `bind_website` and unlinking disconnects the current `WebsiteConnection` generation. This preserves the existing authority checks and invalidates work for superseded connections. Shared GitHub authorization is retained. Settings responses are serialized after the atomic save commits, preserving worker reconciliation without a database lock.
# Startup selection recovery (6 November 2026)

Normal founder company creation applies the same domain ownership policy to staff,
superusers, and other founders. A claimed domain returns `409 company_domain_claimed`
and rolls back the save, including company binding and active selection. Staff status
does not grant another founder's organisation access.

The Chat facade's `auth/me`, founder profile, and company-list GET/HEAD requests are
account-scoped. They ignore a stale company hint while retaining account authentication
and membership admission. Profile and company lists omit owned company rows whose
organisation belongs to someone else, and return a null active ID when that selection
is inaccessible. Company operations and profile/company writes retain explicit scope
and ownership checks. This makes the picker usable for historical invalid selections
without granting access to the underlying organisation.
