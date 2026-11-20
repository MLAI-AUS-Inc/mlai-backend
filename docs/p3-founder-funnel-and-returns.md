# Founder funnel and browser returns

The existing P0 report stays unchanged by default. Add `--journey --json` to
`python manage.py founder_funnel_report --since 2026-09-29 --until 2026-10-29`
to include all eleven P3 conversion stages and nearest-rank p50/p90 seconds from
signup to first draft. The staff-only `GET /api/community-chat/admin/startups/funnel/`
uses the same source query, behind the existing active MLAI-admin, verified own
installation, Django staff and staff-console feature gates. Dates are optional
(default thirty days); the HTTP window is bounded to 366 days and20,000 accounts.
Both paths use a read-only Postgres transaction and forced rollback. The command's
original four audit counts remain reproducible and additive to the new stages.

Conversions are distinct non-staff signup accounts. Startup facts are attributed
to recorded founder accounts, not inferred team membership. Readiness timing comes
from a revision-bound `draft_ready_v1.at` receipt or completed
`package_content_delivery` step. A legacy delivered run without a source timestamp
counts as reached but has unknown timing, never zero. A later article or revision
cannot supply another source's missing first-draft time. Exact timing requires
that no unknown delivered source (across the founder's startups) could be earlier,
using its immutable creation time as a lower bound. Same-run source proof can fill
its own missing clock. Private source bookkeeping retains uncertainty after normal
draft deletion, without changing run or config activity clocks. History is bounded
to 64 competing sources; an overflow remains conservatively unknown. Mutable `updated_at` is not a
completion clock. Current startup interests prove current intent only; their historic
selection time is unknown. No workshop exclusion or production outcome is inferred.

With the existing founder-journey V2 flag on, server mutations emit structured
`founder_journey_event` logs after commit for signed_up, joined, intent,
startup_created, research_done, first_article_started, queued, draft_ready, copied,
published and github_upgraded. Event ids are deterministic per stage/account/org/run
so replay consumers can deduplicate. Payloads contain opaque IDs, source names,
timestamps and elapsed signup seconds, never emails, URLs, customer text or credentials.
Telemetry failures cannot fail the founder action. Actual delivered drafts also save
a private monotonic `pillar_strategy.founder_funnel.first_draft` receipt for new-journey
organizations; normal draft deletion retains it. This key is stripped alongside the
P2 bookkeeping from all strategy DTO/provider payloads. The marker never touches
execution/config update clocks. Consumed free-article billing alone is deliberately
not delivery evidence because owner cancellation after writing can consume it.

The Google connector POST accepts its existing first-party `next`, plus optional
`return_runtime: "tauri"` and `return_path`. The native path must match the selected
company UUID and the same narrow route grammar as the renderer/Rust parser:
articles/settings/create/onboarding/growth; company UUID required; optional article
plan UUID on articles only, googleBaseline=refresh, and known create/onboarding step.
It is signed into the existing one-use OAuth state and returned through the existing
native redirect helper. Unknown/duplicate fields, credentials, fragments, external
origins and escaped paths refuse. No OAuth credential enters the app link.

The anonymous device-auth probe adds `passwordAuthEnabled: true` only while the
existing password-auth flag is on; absence is a refusal to show password sign-in.
The existing flow1 response remains unchanged when passwords are off.

Founder website journey display labels/reasons are translated only for new-journey
organizations. Protocol ids, routes, capabilities, source proof and Advanced/support
evidence stay byte-for-byte unchanged; legacy display copy is preserved.

## Test organisations and the daily founder-health report (plan F1, F9)

A test organisation is marked in `OrganizationContentConfig.pillar_strategy["test_organization"]`
by `manage.py mark_test_organization --organization-id <id> --reason "<why>"` (a dry run
until `--apply`; `--unmark` removes it, `--list` shows every one). The host-only setting
`FOUNDER_TEST_ORGANIZATION_IDS` adds ids on top. `content_factory.founder_test_orgs.is_test_org`
is the one check. A marked organisation is in no funnel, health or re-engagement report,
raises no founder-journey, stuck or staff alert-rule alert (nor counts toward the
provider error-rate rule), never reaches staff Slack (including the goal-scoped
operational alerts), is never swept by self-heal or the background-job digest,
and is left out of the admin usage (revenue and points) numbers. Every guard still applies
to it: spend ceilings, consent, refunds, Content Factory's fleet `sandbox_repo_owners`,
refused-domain list and `max_article_points`.

`manage.py founder_health_report [--json]` prints the daily founder-health report
(read-only): funnel by approved stage, time to first article, setup, article and publish
success, generic stops, top codes by founders affected, `staff_fixing` count and age by
stack, founders stuck over 24 hours by name, duplicate organisations, placeholder and
hosting-builder domains, and the D12 SLOs as PASS/FAIL lines. Staff read the same report at
`GET /api/community-chat/admin/startups/health/` (shown on Chat's founder funnel page). The
`founder_health_report` self-heal sweep posts it to `CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID`
once a Melbourne day after 08:00, only while `ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED` is on
(default off); a post Slack refuses is retried on a later tick, three tries a day at most.
Every section reads at most 2,000 rows of scalar columns and says when it reached that
limit. The funnel reads run documents through `journey_funnel`, so it runs only when a
scalar count finds at most 500 article and research runs in the 90-day cohort; above that
the report says "Funnel skipped" and points to `manage.py founder_funnel_report`. The
first-draft SLO uses the writer's `draft_ready_v1` clock or the completed packaging step,
never a run's `updated_at`; a request with neither is counted as unknown. The
authority-refusal ops alert is not filtered: it is a platform-integrity signal, not a
founder alert.
