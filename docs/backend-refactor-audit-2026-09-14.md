# Backend refactor audit — 14 September 2026

The next refactor should improve tenant ownership, credential handling, execution
reliability, and domain boundaries. Another broad table-deletion exercise would
miss the most consequential debt.

Keep the Django monolith and PostgreSQL. Make small, independently reviewable
changes with explicit owners and acceptance criteria. The existing modular
applications, transactional ledgers, callback receipts, and organisational-memory
runtime provide useful foundations.

## Scope and confidence

Audited the local checkout at commit
ea3dec37fcacb6665262d610492ffd392fdfc4d6, branch
codex/slack-emoji-reactions, including its existing working changes. The initial
snapshot contained 82 modified/untracked files. High-priority findings were
checked against HEAD; they predate the current uncommitted features.

Four parallel read-only reviews covered models and lifecycle, runtime and CI,
legacy features and authentication, and cross-cutting verification. Evidence
comes from source, migrations read as text, routes, deployment definitions,
tests read as text, and current contracts. The supplied September 1 audit was
used as historical context.

No production data or credentials were accessed. No services, tests, migration
commands, deployments, or operational repair commands were run. Source paths
and line numbers below refer to the audited working tree and may move.
Production prevalence, actual worker status, row counts, table sizes, index
usage, and query latency remain unverified.

Static inventory, excluding migration modules from Python source totals:

| Measure | Result | Interpretation |
| --- | ---: | --- |
| Non-migration Python files, including tests | 861 | Tracked and non-ignored untracked files |
| Python files outside test paths | 590 | Approximately 221,460 physical lines, including comments |
| Source-declared concrete models | 255 | AST estimate in models.py files; excludes abstract/proxy models and automatic through tables |
| Model-owning applications | 17 | Not a count of all installed applications |
| JSONField declarations on those models | 329 | Inventory signal, not proof of misuse |
| Migration files | 318 | Includes two existing untracked migration files; none executed |

The 255-model estimate includes the uncommitted Moderator and three reporting
evidence/revision/approval models. It is not a physical PostgreSQL table count
and should not be directly compared with the previous audit's counting method.

## What the previous cleanup already accomplished

The repository contains the orphan-table cleanup
([core.0058](../core/migrations/0058_drop_orphan_tables_from_removed_apps.py)),
stale-content-type purge
([core.0059](../core/migrations/0059_purge_stale_content_types.py)),
MedHack game/unused prediction removal
([hospital.0017](../hospital/migrations/0017_delete_medhack_game_and_prediction.py)),
SEO topic-map/research-session removal
([content_factory.0038](../content_factory/migrations/0038_delete_seo_topicmap_researchsession.py)),
and selector-shadow removal
([org_memory.0025](../org_memory/migrations/0025_delete_selector_shadow.py)).
This confirms implementation in source, not a fresh verification of production
migration state.

The supplied report contains an earlier “keep MedHack” assessment followed by a
later decision to remove it. Current code reflects removal of the game; the
remaining [MedHack URL module](../hospital/medhack_urls.py) contains an
announcement compatibility alias. Do not repeat the original game-table
recommendation.

Preserve the recorded decisions to keep eSafety, Watt/generic hackathons, and
HealthHack. Nothing in this audit reverses those product decisions.

## Priorities

P1 means address before substantial expansion of the affected surface; it does
not assert a confirmed production incident. P2 is the next structural work.
Effort is relative: small means a bounded code/configuration change; medium
means several coordinated changes; large means staged data/contract work.

| ID | Priority | Work package | Effort | Schema change expected? |
| --- | --- | --- | --- | --- |
| A | P1 | Safe company offboarding and tenant reuse | Large | Likely, after a containment fix |
| B | P1 | One encrypted GitHub credential authority | Medium | Yes, separately reviewed |
| C | P1 | Consistent financial-account ownership | Large | Yes, after ownership decision |
| D | P1 | Complete worker deployment inventory | Small–medium | No |
| E | P1 | Reliable test assignment and supported Django | Medium | Review dependency migration plan |
| F | P2 | Isolate slow jobs and recover abandoned work | Medium | Likely for durable leases |
| G | P2 | One workflow transition authority; smaller content modules | Large, incremental | Later explicit relationship changes |
| H | P2 | Platform-owned authentication and identity projection | Medium | Not for initial extraction |
| I | P2 | Remove shadowed Community Home code/routes | Small | No |
| J | P2 | Lock dependencies and deploy the tested artifact | Medium | No |
| K | P2 | Feature lifecycle and retention inventory | Small initially | No until a retirement is approved |

### A. Offboarding leaves history behind before allowing a new owner

The last-company offboarding path claims to leave an empty organisation shell.
Its marketing purge only deletes MonthlyUpdateDraft, ContentFactoryRun, and
OrganizationContentConfig
([founder_tools/services.py:235](../founder_tools/services.py#L235)).
Written articles, keywords, content islands, generated components, snapshots,
and automations are outside that purge.

The organisation is explicitly retained and the company deleted. Ownership is
then inferred from the earliest remaining company, returning no owner when none
remain ([services.py:90](../founder_tools/services.py#L90)). Later registration
reuses an organisation by domain
([services.py:474](../founder_tools/services.py#L474)).
The new founder's own-company checks succeed, and marketing bootstrap exposes
written topics queried by that organisation
([vibe_marketing_views.py:2729](../content_factory/vibe_marketing_views.py#L2729),
[bootstrap:10991](../content_factory/vibe_marketing_views.py#L10991)).

Owner-scoped deletion and first-claim checks exist, but do not prevent this
post-offboarding inheritance. Failed startup-data purges are also collected as
warnings before the response marks organisation data purged
([services.py:316](../founder_tools/services.py#L316)).

**Plan:** initially prevent reassignment of a former tenant unless its lifecycle
permits it; do not infer availability solely from absence of a company row.
Introduce explicit offboarding state, truthful completion, and idempotent
subsystem cleanup/retention handlers. A new ownership claim should receive a
fresh tenant identity unless an explicit transfer authorises continuity.
Separating domain claims from tenant identity may be needed; this is not a
simple change to get_or_create.

**Acceptance:** in an approved disposable database, owner A creates all relevant
artifact types, offboards, and owner B claims the same domain. B receives none
of A's private history or credentials. Partial deletion cannot mark completion
or release the claim. Intentional co-owner continuity remains supported.

### B. GitHub columns named “encrypted” bypass application encryption

OrganizationContentConfig defines github_token_encrypted and
github_refresh_token_encrypted as ordinary TextField columns
([models.py:49](../content_factory/models.py#L49)).
An API path copies decrypted UserIntegration values into those columns
([api_views.py:662](../integrations/api_views.py#L662)); another directly assigns
incoming credentials
([service_views.py:2071](../content_factory/service_views.py#L2071)).
The encryption/decryption field implementation exists elsewhere
([fields.py:166](../integrations/fields.py#L166)).

No corresponding model encryption hook was found. Service-key authentication
restricts callers but does not encrypt these field values. This establishes
plaintext ORM persistence paths, not an assertion about disk encryption or a
credential leak.

**Plan:** use the existing encrypted GitHubInstallation/connection authority for
credentials and renewal. Organisation configuration should retain authorised
installation/repository references. Stop duplicate plaintext writes, stage
compatibility reads, and separately approve any backfill and column retirement.
Review credential rotation coverage: the rotation command discovers encrypted
field types, so these ordinary fields are omitted.

**Acceptance:** synthetic credential tests verify raw database values use the
approved envelope and existing readers remain compatible during transition.
Rotation covers every retained copy. Disconnect respects its authorised scope;
offboarding one company must not revoke a founder's shared installation access
for sibling companies.

### C. Financial records and connections disagree about ownership

Connections permit the same provider account for different users/organisations
([models.py:161](../integrations/models.py#L161)).
Financial records instead have a globally unique provider/account/record key
([models.py:437](../integrations/models.py#L437)).

Xero and bank upserts match that global key and replace its connection, user,
and organisation with the latest sync's values
([external_connectors.py:2731](../integrations/services/external_connectors.py#L2731),
[bank writer:2218](../integrations/services/external_connectors.py#L2218)).
Stripe matches by connection instead, potentially conflicting with the global
database constraint
([finance.py:385](../integrations/services/finance.py#L385)).

The second connection still needs valid upstream authorisation. The problem is
record ownership/reconnect consistency when an account is legitimately shared,
not unauthorised access to arbitrary external accounts.

**Plan:** decide whether records belong to a tenant-owned external account,
individual connection, or explicitly shared account. Prefer tenant-owned account
identity if reconnecting should retain history; model grants separately if
sharing is supported. Align unique constraints, every writer, read filters,
reconciliation references, and disconnect cascades. Measure existing conflicts
before proposing a backfill; do not casually deduplicate financial records.

**Acceptance:** exercise two users and two startups attempting to connect the
same upstream account. The chosen policy either explicitly rejects sharing or
ensures syncing/disconnecting one cannot silently reassign or delete the other's
history. Reconnection and duplicate deliveries preserve intended identities.

### D. Deployment does not manage every defined writer

Compose defines password-email-worker and committee-remuneration, but neither
appears in the deployment writer/startup inventory
([deploy.sh:690](../deploy.sh#L690),
[docker-compose.yml:186](../docker-compose.yml#L186)).
Password reset code queues durable delivery for its dedicated processor
([password_auth.py:84](../core/password_auth.py#L84)).

This deployment path will not start or refresh that worker. Independently
started instances also escape the “all runtime writers” stop before migrations
([deploy.sh:786](../deploy.sh#L786)). The actual deployment and mail backlog were
not inspected. Committee remuneration may deliberately be disabled; it still
needs an explicit lifecycle classification.

**Plan:** one service manifest should drive startup selection, writer shutdown,
rollback, and verification. Record optional enablement deliberately. Add static
consistency checks and queue-age/last-success monitoring for required workers.
Avoid making web readiness depend on every optional integration.

**Acceptance:** every Compose writer is classified, every required worker is
managed, and synthetic pending mail plus a stopped worker produces a visible
health failure. Do not deploy this fix without a separate deployment request.

### E. Regression selection and framework maintenance need attention

CI runs extensive selected suites, including useful PostgreSQL and migration
lanes. Its manual lists omit important existing modules, including
core.tests.test_password_api, founder_tools.tests, roo.tests_coding,
tests.test_jobs_scheduler, and tests.test_content_analytics
([deploy.yml:152](../.github/workflows/deploy.yml#L152)).
This is selection evidence, not measured execution coverage; it does not imply
all related behaviour is untested elsewhere.

**Plan:** deterministically assign discovered tests to lightweight, SQLite,
PostgreSQL/concurrency, and migration lanes. Fail CI when a new test has no
assignment. Keep a visible, owned, expiring quarantine for known failures.
An ever-growing list of selected test methods cannot stand in for coverage of
new features.

The declared framework is Django 4.2.16
([requirements.txt:9](../requirements.txt#L9)).
Django ended 4.2 extended support on 7 April 2026.
The supported 5.2 LTS line is a reasonable upgrade target, with extended support
through April 2028. Verify compatible dependencies and select its current patch
when implementing. Sources:
[Django's end-of-support announcement](https://www.djangoproject.com/weblog/2026/apr/07/security-releases/)
and [support table](https://www.djangoproject.com/download/).

**Acceptance:** all tests have an explicit lane or quarantine; the affected
identity, credential, tenant, billing, and PostgreSQL concurrency checks run on
the supported stack. Inspect and obtain approval for the exact migration plan
before any database-backed test/replay. No upgrade or exploitability assessment
was performed in this audit.

### F. Long-running Jobs work blocks unrelated scheduled work

The generic discovery command invokes twelve runners sequentially, with Jobs
second ([command:77](../core/management/commands/run_scheduled_discovery.py#L77)).
Jobs executes collection and publishing inline
([job_pipeline.py:371](../jobs/services/job_pipeline.py#L371)).
All later reminders, reconciliation, and retention wait for it.

Job claims move to running and stamp claimed_at
([job_pipeline.py:354](../jobs/services/job_pipeline.py#L354)), but no Jobs
heartbeat/expired-claim recovery was found. A killed worker can leave an
unclaimable running row that also suppresses that day's scheduling.

The shared command only records raised exceptions as failures
([command:125](../core/management/commands/run_scheduled_discovery.py#L125)).
Jobs and monthly reminders also return failed status dictionaries
([job_pipeline.py:686](../jobs/services/job_pipeline.py#L686),
[monthly_update_reminders.py:336](../startup_updates/monthly_update_reminders.py#L336)),
which can leave the overall command successful.

**Plan:** make scheduler ticks bounded; dispatch expensive Jobs execution to a
dedicated leased executor. Standardise runner results, failure reporting,
durations, and last successful progress. Add expired-claim recovery, bounded
retries, and idempotent publication before enabling automatic replay.
Borrow the existing [memory runtime invariants](org-memory-runtime.md) without
sharing its domain tables or replacing every queue.

**Acceptance:** a stalled scraper does not delay reminders; killing the executor
recovers work within its documented lease window without duplicate publishing;
returned failure states affect operational health.

### G. Consolidate workflow authority before splitting large files

The working tree has 17,424 lines in vibe_marketing_views.py and 9,298 in
service_views.py. HEAD already has 17,272 and 9,220 respectively. Size matters
here because the same modules coordinate credentials, setup, billing,
discovery, article generation, revisions, publishing, and callbacks.

ContentFactoryRun is documented as canonical and has an organisation FK
([workflow_runs/models.py:33](../workflow_runs/models.py#L33)).
ContentFactoryJob separately stores domain and lifecycle state
([content_factory/models.py:409](../content_factory/models.py#L409)).
WrittenArticle stores a job FK plus a string source_run_id
([models.py:1127](../content_factory/models.py#L1127)).
Callbacks update job/run state in multiple places
([service_views.py:6208](../content_factory/service_views.py#L6208)).

**Plan:** first extract a single workflow transition service used by service
callbacks and browser actions. Then separate setup, credentials, editorial,
generation, publication, and preview delivery modules. Establish explicit
run/tenant relationships after resolving existing records; preserve external
run IDs and wire contracts. Keep job, callback, billing, and notification
records where they represent distinct responsibilities.

The uncommitted editorial and monthly evidence/revision work adds useful
validation and approval boundaries. Preserve it. Consolidate versioned JSON
mutation rules and separate approved human policy from replaceable scan caches;
do not normalise all 329 JSON fields into new tables.

**Acceptance:** callbacks and browser controls share transition rules; duplicate,
late, and out-of-order events cannot overwrite newer authoritative state,
approvals, billing, or tenant ownership. Explicitly authorised retries/resumes
remain supported. Each extraction reduces shared mutation logic, rather than
moving the same dependencies into more files.

## Additional bounded refactors

| Work | Evidence and impact | Acceptance |
| --- | --- | --- |
| H: Move generic authentication into core | Global JWT/cookie auth lives in [hospital/authentication.py:11](../hospital/authentication.py#L11), selected by [settings.py:456](../mlai/settings.py#L456). Identity reads and profile updates duplicate event-team projections in [core/views.py:678](../core/views.py#L678) and [885](../core/views.py#L885). | Compatibility shim preserves imports, token revocation and origin checks; one identity projection preserves response/permission semantics. Optional domain data is separated only after client verification. |
| I: Remove shadowed Home and duplicate routes | [community_chat/urls.py:50](../community_chat/urls.py#L50) and [85](../community_chat/urls.py#L85) register different Home views under the same path/name; Slack deletion is also duplicated. Both defects exist in HEAD. | Preserve the currently selected CommunityHomeView; verify callers before removing HomeView; URL resolution and response/auth contracts have explicit checks. |
| J: Reproducible dependency/build lifecycle | [requirements.txt](../requirements.txt) mixes pins and open-ended dependencies; CI and [Dockerfile:17](../Dockerfile#L17) resolve independently; [deploy.sh:744](../deploy.sh#L744) builds again remotely. Every process image installs Chromium. | Commit a complete reviewed lock, build/test one immutable image, deploy that digest. Evaluate lean browser-worker images after import separation; measure image/build benefit first. |
| Monotonic programme submissions | [Studio:28](../mlai_studio/views.py#L28) and [Victor:32](../victor_ai/views.py#L32) read stage then save without row-lock protection. Concurrent lead/complete requests can defeat sequential “never downgrade” guards. Victor confirmation delivery is synchronous. | Transaction-safe transitions and first-insert conflict handling; durable confirmation intent if delivery is required. Keep programme-specific validation and schemas. |
| Bounded Jobs history queries | [jobs/views.py:161](../jobs/views.py#L161) performs error and top-job queries per run, up to 100 runs. | Shared projection with bounded/prefetched reads; query-count growth no longer scales by two per run. |
| Retire compatibility imports deliberately | [core/models.py:288](../core/models.py#L288) still exports moved domain models for “one release”; active imports remain, including finance. [data_access/registry.py](../data_access/registry.py) eagerly registers cross-domain models. | Migrate runtime callers to owning apps, preserve any historical migration import paths, add import-boundary checks. Domain-owned resource registration retains current field/role allowlists. |

## Retirement and storage decisions

No additional table has been proven unconditionally safe to drop.

| Candidate | Current evidence | Decision/evidence required |
| --- | --- | --- |
| jobs.SeekJob | No in-repo writer found; still read by offline/live-failure fallback in [job_pipeline.py:143](../jobs/services/job_pipeline.py#L143) and [302](../jobs/services/job_pipeline.py#L302) | Identify external writers and replay users, inspect authorised aggregate usage, then retire fallback before proposing archive/drop. |
| Victor and Studio intake | Both remain routed; old programme dates do not establish current product policy | Owner decides whether intake closes, how applicants are served, and how long applications/access audits are retained. |
| Buzz/Discord adapters | Historic experiment docs coexist with current community member and Slack features; bridge deployment is conditional | Identify actual supported clients, adapter enablement, receipts and deletion obligations. Retire adapters individually; preserve current account/Volunteer/Slack contracts. |
| Old Volunteer request/review APIs | Current UI policy and retained compatibility/accounting routes differ | Verify remaining consumers, sunset unused interactions, preserve recognition evidence and ledger history. |
| Duplicate GitHub token columns | Replacement encrypted authority exists, but callers remain | Complete package B and verify backfill/read cutover before removal. |
| Legacy dependency candidates | neo4j and anthropic have no direct Python usage found; Discord relates to adapter lifecycle | Check optional/transitive/runtime use and installation resolution before removing. |
| Raw payload retention | [Gmail attachments:909](../startup_updates/models.py#L909) store base64 and extracted text; runs and delivery records retain request/result/provider payloads | Measure bytes and age, classify evidence versus reconstructible payloads, then choose retention or object storage. Preserve approval snapshots, financial history and idempotency windows. |

Retain historical physical table names where app ownership changed. Renaming
integrations-prefixed startup tables or content_factory_organization provides
little operational value by itself and adds migration/external-consumer risk.
Do not renumber migrations or squash history as cosmetic cleanup.

For any future drop: confirm owner and external consumers, measure aggregate
usage, agree retention/archive, stop writers and retire routes/workers, observe
the deprecation window, then obtain approval for the exact drop migration.
Check foreign keys, generic references, admin, data-access registrations,
recovery commands, and content types. An old last-write date alone is not proof
that a table is unused.

## Delivery sequence

This is an implementation order, not a committed schedule. Keep tenant,
credential, framework, and structural changes in separate reviewable PRs.

1. **Contain correctness risks.** Address A's claim-release behaviour, B's
   plaintext write paths, and C's conflicting-account handling. Establish D's
   complete worker inventory. Add the missing targeted regression assignments
   before treating green CI as a sufficient gate.
2. **Make change safer.** Complete E's test-lane ownership and framework
   upgrade, and J's dependency/artifact reproducibility. Review migration plans
   separately; do not combine an upgrade with bulk schema retirement.
3. **Remove execution coupling.** Implement F's failure contract, worker
   isolation and recovery. Track queue age, expired leases, last success and
   scheduler duration.
4. **Simplify frequently changed domains.** Implement G and H incrementally;
   take I as a small bounded cleanup alongside the community work. Preserve the
   current WIP's revision and approval semantics.
5. **Retire by evidence.** Resolve K's lifecycle inventory and candidate owners,
   then approve specific retention/archive/drop work. Use measured storage and
   query statistics to choose indexing, payload retention or storage changes.

The target ownership map is intentionally modest:

| Owner | Boundary |
| --- | --- |
| core | User identity, sessions, shared authentication primitives |
| organizations | Tenant identity, ownership/lifecycle and domain claims |
| founder_tools / startup_updates | Founder workflows, reporting evidence and revisions |
| integrations | Provider credentials, external accounts and transport adapters |
| workflow_runs + owning product services | Run identity and controlled transitions; domain-specific execution |
| content_factory / content_analytics | Editorial/setup/publishing behaviour and analytics projections |
| community_chat / roo | Member-facing APIs and explicit points/booking/ledger services |
| org_memory | Governed evidence, retrieval, review/publication and its durable runtime |
| Event/programme apps | Their own contracts and lifecycle, isolated from generic authentication |

These are boundaries inside the existing application, not a proposal for new
microservices or a universal workflow/permissions framework.

## Preventing the next accumulation

Maintain a small feature register in the owning repository and cross-repository
map: owner, lifecycle state, supported clients, routes, storage, workers,
enablement flags, retention, last verified use, and next review date.
Compatibility code needs a named consumer and removal condition. An experiment
needs a review date before it gains persistent storage or another worker.

For new persisted features, require an answer to who owns the data, which
service writes it, what happens on retry/offboarding, and which CI lane tests
that contract. Review the retirement register monthly and reserve a fixed
portion of normal delivery capacity for its highest-risk entries.

Measure outcomes: unassigned tests, unmanaged workers, plaintext credential
paths, cross-tenant lifecycle failures, oldest queued work, abandoned leases,
duplicate routes, and compatibility interfaces past their review date.
File/table count reduction is secondary.

## Audit artifacts and validation

This audit adds this report and corrects the owning README/architecture
descriptions of community features versus the inactive Buzz experiment. Those
documents previously contradicted each other about production bridge workers.
No production status is inferred in the corrected text.

Application and migration files were not changed. Existing user changes were
preserved. Static parsing found no syntax errors among the 590 inspected
non-test Python files. Documentation links/paths and the audit's documentation
diff were checked without starting Django. These checks do not establish runtime
correctness; the proposed regression/concurrency acceptance tests remain work
for separately approved implementation.
