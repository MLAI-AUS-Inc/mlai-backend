# C1/C2: one setup consent and automatic article-page selection

14 October 2026. Implementation authorized by the owner's continuous-plan request.
This follows B7 and remains behind default-off rollout flags.

The setup sheet uses a backend-owned plan bound to company, connection generation
and configuration revision. It explains setup and publishing, shows the selected
articles page (changeable), offers automatic first publication or review first,
and records an explicit first-article price consent. The first article uses the
existing Roo points reservation/refund service at its current organization price;
there is no invented free billing bypass. No charge occurs at sheet submission.
D1 must use this consent and one stable billing key before dispatching generation.

Consent is stored in the reserved goal, including goal ID, exact authority scope,
owner, version, accepted time, publish/review mode, page policy/route and CI mode.
A repeat start/resume reuses consent. Changing answers creates an explicit successor
and cancels old work; a webhook can resume but cannot create or alter consent.
Only an authenticated owner POST can submit consent. Foreign/stale scope, malformed
booleans, invalid paths and changed prices are rejected before reserving work.

Required grants are computed for the chosen channel and CI mode. Repository work
needs Contents and Pull requests write. In-repository CI additionally needs Workflows
write and Checks/Statuses read; hosted verification needs no workflow-file grant.
Provider failures stay transient rather than becoming missing permissions. The sheet
has one installation settings link. Existing permission events continue through B6.
Until C3 supplies ARTICLES_HOSTED_VERIFICATION_ENABLED, the plan chooses in-repository
verification and includes its full grants. C3 changes that default to hosted and
owns accepting exact hosted build/browser proof and suppressing the legacy CI
requirements; C1 does not grant proof merely because hosted verification was chosen.

C2 preserves confidence and detail-route relationships from the scan. A clear listing
with child routes and a configured confidence margin wins. Otherwise use the existing
listing, a stable ranked/default listing, or /articles when none exists. Only an owner
who explicitly chooses to pick is asked about genuine ambiguity. Saved explicit paths
always take precedence. Preferences keep their guarded reconfiguration path.

Validation will cover exact scope/goal consent, reuse/change, price and grant matrices,
permission wakeup preservation, TalaThrive/Vyavos/ambiguous fixtures, stale scans,
malformed confidence/paths, the scoped sheet payload and browser start/review choices.
No migration, flag enrollment, production/customer publication or owner operation.


Implementation and validation: consent/page/grant modules, scoped plan inside status,
start-goal reuse/successor rules, trusted B6 source-chain consent rebinding and the
rich permission cache are implemented. First-article targets require explicit
consent and ARTICLES_FIRST_ARTICLE_GOAL_ENABLED; this flag must remain off until D1
handlers are released. C1/C2 flags are false. Existing flags-off grants/status and
ready goals retain their behavior. Answers are rejected before cancelling old work.

All19 database-free CI commands passed. The70 neighboring tests passed; additional
real planner/source-chain/first-goal tests passed (56 combined), followed by46 final
consent/permissions/wakeup tests after the rich cache isolation fix. Model drift,
credential scan, exact639-emitter audit and347-full-module assignment (0 new
omissions) passed. No database, migration, provider write or runtime enrollment.
