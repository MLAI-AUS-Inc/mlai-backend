# E5: durable initial CMS acceptance — 17 October 2026

An initial owner acceptance now combines the exact displayed CMS destinations,
existing author/category consent, review preference and first-article price.
`cms-connect-and-setup` records one original native setup/intent/financial
subject and reserves its accepted amount before any provider traffic. It creates
a paused connection and a pending private key placeholder, which grant no CMS
or Git capability. Legacy automatic publishing/discovery and target/key fields
are cleared when this initial connection is accepted.

The private `cms-initial-connect` stage owns an expiring verification lease.
Each key submission has a separate encrypted `cms-initial-grant` with a 24-hour
deadline. The original native intent, root UUID, accepted price/time, payment
subject and cumulative budget remain unchanged across fresh credentials,
transient failures, process exits and lost replies. Keys and usernames are not
included in status, errors or nonsecret request digests.

Before and after each fixed GET, the worker checks actual current ownership,
company/config/channel scope, the original intent, unrefunded unused hold,
pending grant, budget and winning lease. The public site is first read without
a credential; only the explicitly accepted API destination receives the key.
Actual WordPress identity must permit editing and publication. Ghost requires
an authenticated bounded post read. Neither response is publication proof.

The winning transaction completes the original private key, connects that same
site, records its real identity and returns the original `cms-setup` root to
ordinary discovery. Existing setup then discovers actual native defaults and
promotes the same root/hold into `cms-goal`. There is no second owner start or
payment. No author/category, Git integration milestone or first article is
invented by verification.

Invalid credentials erase the failed proposal and expose only the original
setup's current access-wait epoch. `cms-restore-initial` accepts fresh key input
under that epoch with a new submission UUID. An acknowledged or unknown request
is recovered by reading `cms-initial` with its original acceptance UUID or root
operation UUID. GET performs no verification, reservation or scheduling.
Old erased submissions cannot revive; a restored key cannot alter price,
preferences, acceptance time or previous budget usage.

The scheduler routes initial stages separately from Git reconciliation and
excludes both private actions from public operation detail and journey rows.
Status uses `channel=cms, mode=connect`, an actual backend schedule, an empty
milestone list and no goal UUID until ordinary native setup exists. A paused
provisional summary offers disconnect; it does not advertise the older
synchronous connect/configure actions.

The independent expiry sweep runs with rollout disabled, erases orphaned
encrypted proposals without needing the original owner/parent, and never
substitutes a newer grant. Lifecycle cancellation erases every proposal before
generation/key changes. Stopping or revocation uses the original historical
setup settlement path; verification cannot dispatch a model or CMS write.
Repeated failures and worker loss use B1 occurrences and the original bounded
budget. A stale lease or replaced proposal cannot commit or cancel its successor.

Twenty-eight no-database cases exercise actual encryption and business guards,
WordPress/Ghost identity, original held setup-to-goal promotion, bad key renewal,
lost acceptance/restore replies, stale leases, expiry/orphan erasure, current
owner changes, actual refund/outbox refusal and no-store API reads. The existing
disposable database round trip now has a second variant beginning with this
single acceptance, injecting a worker exit, and retaining the original paid
source through the existing one-create publication and lifecycle checks. That
variant is assigned to remote CI; it has not run against a local database.
The original 1,478 website no-database tests pass, model checks report no changes,
842 production failure emissions are audited and 590 test modules are assigned.
Media fixtures now share one controller clock instead of relying on a
one-second scheduling allowance under host contention.

Follow-on transaction review found that the between-GET financial guard could
request PostgreSQL's ledger row lock without an owning transaction. Every
executable guard now enters a short organization transaction before reloading
its lease, current owner and original spend; provider I/O stays outside those
locks. Executable scope also refuses a missing transaction explicitly. A new
no-database regression checks this boundary, and an actual TransactionTestCase
variant runs the same paid round trip without a test-runner outer transaction.
That variant is explicitly assigned to the disposable PostgreSQL lane as well
as the ordinary database suite.
All 1,479 website no-database tests pass after this transaction correction.

`ARTICLES_CMS_ONBOARDING_ENABLED=false` and all related rollout flags remain
off. The combined initial Chat form/decoder, real provider/browser acceptance,
explicit paid-source transfer, hosted fallback, additional connectors,
revisions/unpublish and the actual nightly test zoo remain unfinished. This
runtime slice does not certify a live website or the 90% nightly target.

The PostgreSQL transaction variant now explicitly enrols its actual synthetic
organisation in both runner and failure-policy gates; sequence IDs cannot rely
on the default fixture allowlist. Initial quotes accept a fresh configuration
with its database-default nullable repository and canonicalise the accepted
provisional configuration to an empty repository. Two database-free regressions
cover that fresh configuration through quoting and identity verification. The
full database-free journey selection passes 1,481 cases. The next PostgreSQL
run remains the authority for the transaction round trip.


18 October transaction audit: a real `TransactionTestCase` in disposable CI
exposed the saved-policy hold check running outside an owning transaction.
Regular `TestCase` transactions and mocked connection state masked the stop
before setup promotion. Saved-policy guards now lock the organisation, current
setup lease and original spend in short transactions around the unlocked
provider GET. The same audit reproduced a paid credential-recovery/status read
locking its ledger outside a transaction; its actual root and original spend
now share a short owning transaction as well. HTTP remains outside those locks.
Regressions assert actual financial lock depth and unlocked provider traffic;
87 focused initial/setup/access cases and all 1,482 database-free journey cases pass. The real transaction regression remains in both disposable SQLite and PostgreSQL CI lanes.
