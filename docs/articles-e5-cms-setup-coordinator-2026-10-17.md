# E5 durable native setup coordinator — 17 October 2026

One authenticated owner acceptance now reserves the original setup choices and
first-article amount before provider discovery. A private `cms-setup` operation
and immutable `cms-setup-intent` are saved with that hold in one organization
transaction. Closing the page or losing its response does not discard the work.
Repeated request IDs and another click reuse that same site operation and price.
Different saved choices cannot mutate the accepted request or create a debit.

The background website scheduler claims an expiring setup lease. It reads actual
native author/category identifiers, saves the exact accepted publication mode,
then promotes the same operation into a real `cms-goal`. Its UUID, payment key,
held receipt, original acceptance/consent time and budget lifetime survive that
promotion. No native goal, policy, milestone or proof is fabricated beforehand.
An already accepted exact current policy needs no extra configuration reads.

Worker loss after policy commit resumes from that actual original private
acknowledgement without repeating discovery. Provider refusals preserve B1
causality; one occurrence is recorded per real attempt or expired worker lease.
Polling a scheduled retry does not spend another attempt. Repeated failures
escalate through the shared policy. Only real credential failures request owner
access; strategy/recovery requirements remain recorded terminal work for the
remaining channel fallback implementation.

Budget accounting uses explicit backend setup allowances, separately from CF's
actual run reports. A claimed execution lease reserves an active-time bound of
180 seconds; each actual defaults GET admission reserves a provider-time bound
of 180 seconds. These are conservative admission bounds, not measured CPU or
network durations. Lost allowances remain accounted. Later CF reports add that
original backend source once, including repeated/stale callbacks; unknown CF
measurements remain unknown. Rejected work does not invent an execution lease.

Cancellation/revocation fences setup before changing keys or generations and
queues one durable unused-hold credit. The credit worker does no provider or CF
work and remains active after rollout is disabled. An actual dispatch/outbox
blocks an unused-setup credit. A promoted goal uses native goal settlement.
Erasure retains the original private financial proof until credit is confirmed,
then erases the setup planning data. A real committed credit is reused after a
lost acknowledgement. Private settlement/intent records stay outside Git
reconciliation and public operation-detail/latest-operation surfaces.

## Owner API and status

`POST /api/v1/my-startup/vibe-marketing/website-connection/cms-setup-articles`
requires the explicit current `company_id`, `website_connection_id`,
`connection_generation`, a UUID `client_request_id`, the current
`setup_quote_digest`, `review_policy` (`auto` or `review`), and integer
`expected_cost_points`. `publication_confirmed`, `native_defaults_confirmed`
and `billing_confirmed` must each be actual `true` booleans. Empty Git fields
are unnecessary; supplied optional connection fields must match the actual CMS
connection. Public JSON cannot supply a private lease or financial selector.

The response acknowledges the accepted operation and includes its current safe
`websiteStatus` when readable. Before promotion, that status has `channel: cms`,
`mode: setup`, the actual operation ID, `goalId: null`, empty milestones/evidence
and the actual schedule. Promotion changes the mode to `goal` under the same ID.
Status reads perform no provider call, debit, scheduling or evidence repair.
Their final owner/site check prevents a concurrently revoked owner from receiving
old diagnostics. New quotes disappear once an original setup exists and when
the independent setup rollout gate is off.

## Validation and rollout

The 1,356-test website no-database suite passes. Focused tests cover original
price/zero, retry and lost-response replay, same-identity promotion, owner/scope
changes, worker loss, actual discovery reads, refunds after revocation/flag-off,
privacy erasure, budget carry, secret-free status and the owner route. All 582
test modules are assigned; 822 production failure emissions are audited; model
dry-run reports no changes. Disposable CI extends the real ORM synthetic
hold/outbox/publish/erasure flow to begin with durable setup and verifies that a
changed current price does not reprice the original hold. Its result must be
checked on the PR. No local DB, real debit, provider write, model call or deploy
was used.

All rollout flags remain off, including `ARTICLES_CMS_SETUP_ENABLED=false`.
This requires the paired Chat setup-mode decoder/form before enabling it.
Reconnect/rebinding, completed-proof freshness, strategy/hosted fallback and
actual CMS/SEO/full-zoo acceptance remain release prerequisites. This change
does not establish the nightly 90% live-article success rate.
