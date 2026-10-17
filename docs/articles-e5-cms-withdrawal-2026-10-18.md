# E5: explicitly unpublish an owned CMS article — 18 October 2026

An owner can request that a previously published MLAI article return to a CMS
draft. The command targets the provider ID from the original confirmed private
publication journal. It changes only status, retaining the article, media,
author, categories and tags. It cannot create a replacement article, request a
model, reserve points, refund a used generation or erase the original historical
first-publication achievement.

The current owner must confirm the actual article and original publication
identity. Admission must refresh founder/company/config/connection generation,
current credential and the original owned publication. Each accepted request has
an independent UUID, durable lease and fixed verification budget. No browser or
worker JSON grants provider permission. Rollout is separately disabled.

Admission must fence any active native goal/publisher for this same original
writing source before the status mutation, so its repair path cannot republish
the article. Unrelated goals are retained. A completed achievement stays a past
fact. The private confirmed post journal and fresh provider ownership read bind
the target; an uncertain original create supplies no target ID. This explicit
zero-cost command must not reuse a paid generation admission or alter its price,
reservation, source or charge. Expired live-health evidence is distinct from
original provider ownership and current CMS permission.

Before the single status write, read the actual provider ID and verify its
original ownership markers, body, title and native fields. Changed or foreign
content is refused. WordPress uses its documented
[post update](https://developer.wordpress.org/rest-api/reference/posts/), sending
only `status=draft`. Ghost uses its documented
[post update](https://docs.ghost.org/admin-api/posts/updating-a-post), sending only
draft status and the freshly read `updated_at` collision token. Omitting related
fields prevents accidental replacement of author/tag relations.

Persist issuance before HTTP. A lost response or worker restart must recover
with GET of that exact ID, never a second status write. A still-published record
after issuance is uncertain: it could be a lost write or an owner republishing
the article. After eight bounded reads, preserve that uncertainty for inspection.
An already-owned draft needs no write. Do not alter a new owner edit or issue a
hidden retry. A separately explicit future retry needs its own current owner
decision and must permanently fence the predecessor.

Provider draft state is a distinct proof from public withdrawal. The coordinator
must recheck current authority/lease around each request, confirm the original
exact public URL is no longer served, and record those observations separately.
It must not infer public removal from a timeout or a generic page. Revocation,
rollout pauses, stale leases and cancellation fence all future effects. Published
content is retained on disconnect unless this separate explicit operation has
completed. Historical achievement remains a past fact; the current live-health
reader must expose withdrawal rather than advertising a live article.

Implementation order: closed intent/single-write recovery contract; current-owner
admission and worker; actual public-absence proof and status; owner UI and scoped
browser/ORM regressions. All tests use synthetic responses and disposable remote
databases. No live CMS action or rollout is authorised by these notes.

The closed contract now binds the original confirmed publication and one fixed
provider ID, keeps status-only writes separate from ownership/public-absence
proofs, and limits unknown recovery to eight GET observations. Eleven synthetic
cases plus the existing fifteen post-journal cases pass without database or
network access. The first checkpoint covered the contract only; the coordinator and UI follow it below.

A separate credential-free, DNS/TLS-pinned exact-URL fetch now accepts only an
actual 404/410 after the owned provider draft observation. It preserves the
validated WordPress plain-post query, follows no redirect, reads no error-page
body and exports no transport exception or credential. The closed proof binds
site, owner generation, original post, withdrawal UUID, provider draft digest and
checked time. Current proof has a five-minute window; a historical proof grants
no new request permission. Eight additional cases pass, for 34 focused cases
including existing post-journal regressions. The coordinator continuation below adds actor/lease coordination, fencing and owner routes. The owner interface and real provider acceptance remain unfinished.


The backend coordinator is implemented behind `ARTICLES_CMS_UNPUBLISH_ENABLED=false`.
Its owner POST accepts only the original confirmed publication, exact intent
digest, original public URL and explicit confirmation. An actual owner nonce
GET recovers acceptance without repeating a write, including after the original
deadline. Neither path quotes or touches a wallet. Admission fences active
publishers/goals only for that writing source, retaining completed achievements
and unrelated goals. The current key and selected owner are re-read at every
effect and fenced journal commit.

The independent 15-minute deadline, durable provider/public GET allowances and
three-minute private leases survive restarts. The status update is persisted as
issued before HTTP; lost/malformed replies can use only the original ID's GET
reconciliation. Eight still-published observations stop without resending. A
second identical failure or worker loss stops; credential failure waits for
renewal of the same actual key without resetting the journal/deadline. Provider
and public requests run outside owning database transactions. Generic operation,
journey and connection projections exclude this private action; purge erases it.

Owner metadata exposes the title, original URL/digest and actual request/schedule,
without the body, API origin or key. Goal status removes the live link during a
withdrawal and records the verified result separately from the earlier passed
publication checklist. A past absence proof does not authorize another request
or certify the future state of an owner-edited CMS. Rollout pauses produce no
provider request.

Thirty-six synthetic WordPress/Ghost runtime cases pass, covering lost replies,
lease replacement, repeated worker loss, exact-ID recovery exhaustion, public
absence limits, owner revocation, no-store nonce reads, and preserved financial
and historical facts. The full real-ORM initial/setup/publication fixture now
also exercises a committed withdrawal with a lost reply, one charge, no refund,
retained achievement and private erasure. That fixture is syntax checked only
locally and must pass disposable SQLite/PostgreSQL CI. Owner UI, real provider
acceptance and rollout remain unfinished; no actual CMS, paid generation,
production service or local database was used.


A new explicit nonce is permitted after a proven completed withdrawal (for
example, after the owner republishes the same post). It cannot replace an
unfinished, failed or uncertain original request. Each accepted command retains
its own deadline, read allowances and single status-write journal. The old
receipt remains unchanged. Ghost's
[Admin URL serializer](https://github.com/TryGhost/Ghost/blob/main/ghost/core/server/api/endpoints/utils/serializers/output/utils/url.js)
can return a draft preview URL; withdrawal ownership ignores that draft URL and
verifies public absence only at the saved original published URL. Synthetic
preview tokens never enter a receipt or a public request.
