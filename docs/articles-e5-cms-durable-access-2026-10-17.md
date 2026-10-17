# E5: durable original CMS access recovery — 17 October 2026

An owner restoring a CMS key must not keep a browser open while a provider is
unavailable. `POST website-connection/cms-restore-access` accepts one exact
original setup/goal and encrypts the proposed key before any provider request.
Its response identifies the actual private journal. A later owner GET can
recover that acceptance without sending the key again. The old synchronous
connect endpoint remains compatible; the new Chat recovery form uses this queue.

The owner read adds a failure epoch derived from the original recorded B1
access failure occurrence. The closed request binds that epoch, owner/site,
configuration/channel revision, original root, current private key operation and
principal. A hash only checks metadata consistency: every claim, GET and commit
independently reloads current ownership, original unrefunded payment, native
authority and current key. A different CMS account/site is not a renewal.

The private worker reads the public site without a key first, then verifies the
same actual publishing principal using a fixed authenticated GET. Both requests
run outside owning transactions. The winning private lease commits the
replacement into the original key operation and wipes its proposal atomically.
It preserves the original article, hold, policy, native post and single-create
journal. It does not create a model run, CMS author, post or a publishing grant.

Page closure and missing replies do not stop verification. An abandoned worker
lease is counted once. Transient errors retain the same encrypted proposal;
repeated identical failures escalate through the shared B1 policy and stop.
The original site's wall, compute, cost and attempt limits apply before more
work. Real backend lease/GET allowance sources remain separate from real CF
reports, including unknown counters and later repeated/stale reports. No fake
CF run or reset allowance is introduced. Terminal recovery queues only the
original existing settlement authority, without performing a credit itself.

`ARTICLES_CMS_ACCESS_RENEWAL_ENABLED=false` independently pauses execution and
new acceptance. A rollout pause during HTTP retains the encrypted request.
Current parent setup/goal workers defer old-key retries while it is pending;
owner status shows its actual next check. The proposal expires after 24 hours,
and bounded expiry erasure runs even with execution disabled. Disconnect,
offboarding and purge also erase it. Its action is excluded from all generic
owner operation/detail and journey surfaces.

Threat cases covered include replaced credentials/ciphertext, wrong principal,
missing publish permission, revoked/changed owner, a later access-failure epoch,
late HTTP after lease expiry, newer lease wins, lost acceptance/completion,
worker loss, no lock during HTTP, route privacy, parent barrier, paused rollout,
expiry and repeated-failure/budget stops. Disposable remote ORM CI extends the
already-issued native publication case to queue, verify and erase a synthetic
replacement while keeping one original key, payment and post. Local checks use
the no-database runner and model dry-run only.

All rollout flags remain off. The paired Chat form must avoid the generic
request-body mutation cache: retain only scope metadata/UUID, clear secret
inputs on close/completion and recover uncertainty through owner GETs. Initial
CMS onboarding, account/site transfer, hosted fallback and real CMS/nightly
acceptance remain release prerequisites.
