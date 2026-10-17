# E5: scoped owner CMS access form — 17 October 2026

Chat needs the actual credential destination and confirmation of an accepted
replacement key without exposing a credential or replaying an uncertain POST.
The owner API now provides `GET website-connection/cms-access?operation_id=…`.
It loads the original native setup/goal through its owning private authority,
current company/site/policy and current private credential. The UUID locates a
record; it supplies no permission.

Only an actual `needs_user` / `connect_cms` wait offers form metadata: exact
platform, public/API origins, original operation, connection generation,
configuration revision and current closed publishing-channel scope/revision.
Working, cancelled or manual article-review states cannot offer credential
input. The API also returns the actual current connection request nonce and
scope as `cmsConnectionReceipt`. The same receipt accompanies successful owner
`cms-connect` responses. It can acknowledge a lost reply without repeating
provider discovery or exporting a key. It does not grant model/publication work.

Reads refresh ownership, selection, original operation and key before/after
projection, and again after optional status collection. A concurrent owner or
scope change returns a bounded refusal. Responses are private/no-store. They
contain no credential, username, native principal, financial key or encrypted
payload fingerprint. No HTTP, scheduling or write is performed by this read.
The original native goal's actual key-recheck schedule is now exposed alongside
its access wait; manual article review still has no polling schedule.

Ten new access-form cases and one goal-status case cover original setup before
policy discovery, original paid goal, acknowledged renewal, null/manual/working
states, foreign scope, malformed nonce, revoked/concurrent ownership, rollout
pause and the GET route. The website no-database suite and remote ORM result are
recorded in the PR. Disposable ORM CI additionally reads the real original wait
while its post is issued, renews the same key, confirms its nonce and wakes only
the original access wait. No local DB, real provider, paid model or deploy runs.

All rollout flags remain off. This requires the paired credential-only Chat
form and its actual schedule decoder before enabling it. The browser must keep
secrets out of the generic mutation identity/cache, storage, URL, diagnostics and
router drafts. A scoped UUID alone can persist uncertain request identity.
Initial CMS connection UI, explicit cross-account/site transfer, hosted fallback
and actual CMS/nightly acceptance remain release prerequisites.
