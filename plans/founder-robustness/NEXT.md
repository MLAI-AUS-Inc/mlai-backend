# Founder robustness continuation checkpoint

Updated 2 November 2026, Australia/Melbourne. Full completion remains open. The 31 original phase 0–5 bullets are retained in requirements.json; original success targets need fresh article/setup observations.

## Verified production baseline and work completed

- Backend release 6ae4094d. All 12 backend application containers have dual write true and Run reads/shadow reads unset. Django confirms both read switches false.
- Initial population: 2,036 Jobs, 4,557 Runs, 99 articles. Existing-run-only backfill filled 1,782 Runs / 10,785 columns and linked 51 articles. Created no Runs. Changed no Run status or updated_at. A repeat dry run proposed zero writes.
- Post-backfill comparison: 254 missing Runs, 52 identity mismatches (50 domains, 43 Slack users), zero remaining empty delivery phases. The domains are empty on the Run; 41 Slack mismatches are empty on the Run, two have differing populated values. Preserve Run authority rather than overwriting it.
- Missing Jobs are January–August history: 187 terminal, 67 non-terminal; four have articles, two have charged status, 48 deferred. Thirteen lack a current organisation; five more lack a marketing config. Do not blanket-create, delete, refund or hide these records.
- CF runtime inspection confirms eight robustness flags true in all nine relevant app/verifier containers. CF_RUN_MAX_MODEL_USD and CF_REVIEW_PASS_MAX_USD are unset. No finite dollar ceiling is enforced yet.
- CF nightly lifecycle run 41347716093 failed admission prerequisites: SANDBOX_CONFIGURATION and ONE_CLICK_TEST_TOKEN were absent. No qualifying acceptance night ran.

Counts-only operation scripts/logs are saved outside the repositories at /Users/samdonegan/Documents/Code/founder-robustness-evidence-20261102. No founder content, credentials, domains or Slack identities are printed in those reports.

## Active implementation

The owner approved workflow_runs/0008_contentfactoryrun_job_identity (four nullable Job identity/timestamp columns), disposable test migrations and normal backend deployment. See job-identity-migration-proposal.md for exact scope. Implement, test, review and deploy; refill only the new empty fields, compare again, and keep real reads off while historical coverage remains unresolved.

The new compare command --check prevents vacuous parity success. Its unit tests run with database and network denied. Default report-only behavior stays compatible.

CF branch codex/robustness-context-pricing-20261102 at 1609463d adds opt-in context-tier pricing. Independent review found no actionable defects; 107 focused tests passed. No actual rates, limits, flags or releases changed. Finish its draft PR and validate provider configuration before enabling finite ceilings.

## Work owned by the founder journey continuation

The separate active chat "Plan remaining implementation work" is recovering P2 packages in /Users/samdonegan/Documents/Code/founder-journey-worktrees. It owns contracts, resilient transport, escrow/dispatch, source re-pin, self-heal and publish changes. Its recovered branches predate backend 6ae4094d; merge current main into the integration base before shipping so the deployed Run columns, mirror, backfill and migrations are retained. Do not overwrite their files from this branch or duplicate their work.

## Next gates

1. Complete approved identity migration and validation, then deploy using the specific pending migration-plan digest.
2. Refill identities and timestamps; repeat global and post-deployment comparisons. Verify natural post-activation Job writes; the last observed Job update predates this continuation, so backfill proof is not new-traffic proof.
3. Resolve historical missing-Run retention and reader eligibility, including purge evidence and outstanding billing. No blanket --create.
4. Require actual shadow traffic through all switched paths before enabling real reads.
5. Integrate the peer P2 packages and finish run-only writes, authoritative events, recovery, client adoption and original outcome measurements. Retire code only after those stages are verified.
