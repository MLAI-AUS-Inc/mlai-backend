# Content Factory Job to Run cutover

ContentFactoryRun is the canonical backend fact owner only for a newly admitted execution whose private `run_owner_v1.authority.backend_facts` is true. An admitted protocol-one execution also owns lifecycle through typed CF events. Unmarked historical and reservation-only rows retain the existing implementation. This is a source implementation contract; production flags, providers, migrations, sends and releases remain separately held.

## Admitted execution ownership

The explicit `run_backend_facts` service locks the canonical Run before its compatibility Job and validates partial patches. It projects an output Job atomically, preserves unrelated metadata keys, and refuses conflicting edits or replacement billing lineage. An owned transaction reads current Run facts before yielding its Job-shaped view. A missing compatibility row is rebuilt from canonical facts with the copied identity clocks. A long logical request key remains exact in admission and ledger history; a bounded alias is used only for existing 100-character columns.

The caller inventory routes all ordinary Job writers through this service. The binding rename remains its explicit identity operation; standalone scaffold creation has its own reviewed prerequisite gate. Reverse mirrors, historical backfill/undo, admin edits, generic teardown and raw ORM/collector history deletion cannot become alternate owned writers. The feature switches govern new admission only: switching them off never returns an already owned row to legacy authority.

For an event owner, legacy snapshots, callbacks and the old strict v1 delivery transport enter the observation adapter before the legacy transaction or side-effect decorators. They do not project lifecycle, steps, approval, original requests or execution clocks. The v1 wrapper must preserve its exact creation admission and execution ticket. The independent typed artifact reader verifies the current complete scope and actual requester, the canonical current article body, and actual GitHub pull-request identity and head before projecting delivery facts. Its protected receipt retains proved historical value after later failure or a new ticket; completed status and a completed packaging step alone do not consume a free article or mark a first delivered draft.

No-start owned refunds remain held unless the private original-dispatch settlement context proves CF's exact durable `closed_no_effects` tombstone for the current original key, payer, debit and full charge family. A missing-key observation, timeout or failed callback is insufficient. The existing original-charge controller performs any authorized effect, without changing original Run requests or clocks. See [dispatch settlement](run-dispatch-settlement-v1.md) and [typed event authority](run-event-authority-v1.md).

Started terminal settlement independently drains authenticated typed history and requires CF's exact current-ticket closure of leases, provider calls and effects. It checks the original obligation and every carried family member before invoking the existing refund or grant-release controller. Actual refund or released-grant history prevents every sibling from continuing that obligation. Malformed private history/settlement metadata remains unchanged and held; separate transport cadence prevents one bad row from starving later valid work.

Research delivery is a separate verified benefit. The closed typed topic evidence must match the canonical retained choices and original TOPIC charge or grant. A later confirmed ARTICLE has its own sealed debit and immutable child key. Retained topic choices block settlement of their original research benefit without falsely marking the separately charged article delivered. Explicit confirmation delivery mode is carried into the new article command and rechecked with current source consent before its debit.

An admission-on setup approval may create a new owned child from an actually unmarked scan only with CF's authenticated retained-source descriptor and fresh actual requester, website and source checks. The immutable descriptor is part of the child's original request digest; the parent gains no synthetic admission. Parent echoes remain absent in that legacy case. An old plan whose source differs from GitHub's current head requires a fresh scan. Admission-off genuinely legacy behavior stays unchanged; an already recorded owned child replays its original saved descriptor and ticket.

## Current review and retained benefits

Approval uses a protected current-ticket consent receipt bound to the independently verified canonical body revision, body hash, quality, exact preview, requesting actor and authenticated parent-only acknowledgement. The public `approvalState` is derived from that receipt. Approve does not enqueue publication or change the Run lifecycle. Publishing remains a separate explicit command with a separately admitted child and fresh destination/publisher checks.

The typed artifact bridge accepts body, ready, quality and PR evidence with their original creation tickets. Current approval quality additionally matches the closed independent `review_metadata` read and the current ticket. A fresh successful evidence read that omits quality invalidates only approval-purpose quality. Historical delivery and earned free-article benefit remain. Preview POST and DELETE carry both current admission/execution echoes; stopped or settled work cannot start a preview, while exact cleanup remains available.

An explicit Resume offer requires current typed failed/blocked state, a registered transient `retry_same_step` policy, original sealed budget, a recent validated current-ticket budget observation below cap with no uncertain calls, current consent/principal and an unsettled original obligation. It is an offer to request continuation; the endpoint and CF retain fresh budget/effect authority. Editorial repair, source repin, needs-user and unsupported failures have their own paths.

The nightly island-refresh service has a separate exact versioned scheduler scope. Its ticket gate rechecks the original budget and source-free admission, current research eligibility, keywords, roster/frozen state, pause and engagement policy, stop intent and current ticket. It grants neither a founder identity nor repository, publication or article-payment authority. Other scheduler services and old unversioned admissions remain held. See [nightly recovery](articles-r3-nightly-recovery.md).

New-cohort article-review successes retain their existing response and add a scoped `articleReview` version-two envelope. Its resolved parent company/run IDs remain exact even when a mutation declares a child handoff. Current edit/consent access comes from the actual command rights, frozen/stop/settlement state and current review proof. Candidate failures use actual map IDs and closed server copy; the existing selected-run scheduler receives a bounded cadence from actual pending review work.

The existing Run GET supports a pure `view=route` descriptor for the new cohort. It resolves the actual company and scoped, visible Run before classifying only article workflows as `article`; setup, scan, discovery, nightly and other advanced work retain their advanced view. This early read cannot poll providers, project metadata, merge pull requests or settle charges.

An `Idempotency-Key` on first-article start binds the actual owner, company and complete submitted body to a durable backend command and its original server request key. The approved local dispatch data is retained before the debit/outbox transaction; no provider call holds that preparation lock. A terminal, refunded or lost-acknowledgement retry reads the same original Run/outbox. Conflicting bodies refuse, concurrent claims cannot allocate a second command, and current owner/frozen consent is rechecked under the actual debit lock. Unkeyed callers retain their existing behavior. Portable scope is normalized before the first admission digest, so dispatch cannot change a sealed null repository to an empty string.

## Retired execution writers and repair callers

| Boundary | Event-owned behavior | Existing legacy behavior |
| --- | --- | --- |
| Snapshot, callback, service sync and telemetry | Typed observation adapter or closed refusal before old side effects | Existing projection |
| App, canonical HTTP and SDK controls | Current ticket, explicit user intent and typed journal; parent consent is backend metadata | Existing control contract |
| Scan, revision and publication children | New admission before outbox/HTTP; original parent input and charge retained | Existing routing when admission is off |
| Provider merge, preview and review metadata | Fresh result merge with current-ticket checks; no execution clocks | Existing metadata clocks |
| Cancel, deny, disconnect and late dispatch revoke | Durable backend intent and typed command; CF events own terminal state | Existing local cancellation |
| Goal, doctor, wake and P2 failure-policy repair | Current verified observations; legacy repair, counters, rung changes and implicit resume defer to the event owner | Existing coordinator and repair policy |
| Money and entitlement | Exact original-family closure and actual debit/grant proof; historical delivery blocks no-benefit settlement | Existing original-charge controllers |

The reviewed writer manifest and its gate bind every manually classified AST witness to its complete enclosing function context. Separate control contexts cover repair callers that do not themselves write lifecycle fields. This is a bounded drift check alongside actual endpoint/financial tests; it does not claim to discover arbitrary dynamic SQL or every future alias. Original writer identities, domain exclusions, retirement guards and proof references remain in the manual review record.

## Rollout sequence

1. Verify the deployed revision and effective dual-write setting in web and workers. GitHub variables alone are not runtime proof.
2. Run `python manage.py backfill_run_job_fields --dry-run`. Review conflicts, missing and ambiguous records and proposed article links.
3. Apply the reviewed existing-run-only scope with `python manage.py backfill_run_job_fields --apply --batch-size 500`. Do not add `--create` routinely. Repeat the dry run to establish idempotence.
4. Run `python manage.py compare_run_job_fields --check`. A passing result requires a nonempty, completely matched population. Missing, unfilled, ambiguous, orphan, incomplete-identity and mismatched records block the check. Report-only use without `--check` keeps its previous exit behavior.
5. Resolve every blocker. A date-filtered comparison covers only that subset; the global read flag is not date-scoped. Do not waive missing historical records without enforcing the same eligibility boundary in readers.
6. Enable shadow reads only after parity, then exercise both Slack thread resolvers and the billing/entitlement read guards. Empty mismatch logs without traffic are not evidence. Include equal-rank routing candidates with different Job/Run timestamps.
7. Enable Run reads after observed shadow parity and verify the actual runtime settings. This switches only the existing five read sites; it does not migrate all remaining Job consumers.

`--updated-since YYYY-MM-DD` starts at midnight in the backend's configured timezone, not an exact activation instant. For precise post-activation proof, inspect timestamps against the recorded deployment time without printing founder identities or content.

## Identity and timestamps

The Run's domain and slack_user_id can differ from the old Job's. Preserve legacy read behavior in dedicated job_domain and job_slack_user_id columns; never rewrite Run identity to force parity. The compatibility view also needs job_created_at and job_updated_at for the ordering that used to operate on Job timestamps. Null means not mirrored; empty strings remain valid copied legacy values. Complete the approved identity migration and backfill before switching reads.

## Missing historical records

Do not create live or awaiting Runs from old Jobs. A newly runnable record can be picked up by reconciliation or shown to a founder. For finished Jobs, establish canonical execution identity, current ownership, purge history, billing settlement and article references before allowing creation. A current organisation alone does not prove that its old marketing data was not purged.

Historical `--create` accepts only the explicit phases `pr_opened`, `needs_review`, `confirmed`, `cancelled`, `completed`, `error`, `failed` and `denied`. It excludes `blocked`, `auth_required`, all awaiting phases and unreviewed wire aliases. The shared lifecycle contract still classifies blocked work as stopped; that does not grant permission to recreate it. `create_skipped_live` includes all phases outside this history allowlist. Filling facts on an existing blocked Run remains supported and does not change its lifecycle.

Preserve ledger and article references. Never delete a charged Job or recreate purged content just to make the parity check pass. The current creation command lacks an organisation allowlist/purge exclusion, so it is not suitable for blanket repair of a mixed historical population.

## Rollback

For unmarked legacy rows, disable ARTICLES_RUN_JOB_READ_ENABLED to serve the authoritative Jobs again. Keep dual write during investigation unless the mirror itself is faulty. Disable shadow comparison separately if it is the source of a problem. An admitted fact owner cannot roll back to a competing Job writer; retain its canonical writer and compatibility projection while holding new admission.

Do not use `backfill_run_job_fields --undo --apply` as a flag rollback. It blanks mirrored columns and unlinks articles broadly, and dual write can refill records during the operation. Any undo needs its own reviewed scope. Preserve additive columns when rolling application code back; dropping them is a separate destructive migration.

After Run becomes the write owner for a field domain, rollback additionally requires its one-way Job compatibility projection to be current. Never permit competing Job-to-Run and Run-to-Job authorities for the same field.

## Remaining rollout and removal gates

RunJobView remains read-only and cannot replace mutable Job objects or their relations. The paired CF source, backend command producers, client command keys, PostgreSQL concurrency and legacy regression suites must pass on the final combined tree before a separately authorized rollout. A local source checkpoint or passing fixture is not production activation or a live provider proof.

Existing unmarked runs still use their historical observational events and snapshots. Protocol-one admitted runs use the typed reducer and retired-snapshot adapter described above. Job-table removal remains separate and needs validated retention/restore procedures and approval for its specific migration; this implementation adds no migration and does not enroll or rewrite historical rows.
