# E5: recheck old native publishing access — 17 October 2026

Reusing an old CMS publication policy could start generation with an already
expired access milestone. Durable setup now reuses actual discovery only when
it is at most a day old. Otherwise, its private lease performs one authenticated
GET to verify the original WordPress publishing identity/capabilities or the
selected active Ghost authors. It preserves the existing policy, native author,
category/tag settings, review choice, configuration revision and original price.
It never selects a replacement author or creates a provider resource.

Each GET reserves a real provider allowance. The owner, original held payment,
current policy and lease are rechecked before the request, after it and under
the organisation lock before recording its observation. A transient refusal
uses the existing bounded retry policy; a credential refusal retains the real
owner access stop. Lost promotion after observation commit reuses that exact
observation instead of repeating discovery or charging again.

The private policy receipt stores a closed, scoped access observation with the
actual checked time and consistency digest. The evidence reader validates it
against that actual current private policy and credential. Without an explicit
observation it uses immutable policy creation time, so an unrelated receipt or
cleanup update cannot freshen access. Removing a new observation falls back to
the actual old creation time. Neither record is provider or model permission.
The one-day setup bound leaves room within the existing two-day active wall
budget before draft preparation's seven-day observation limit; owner review
waits retain the separate completed-preparation semantics.

Validation: seven new regression cases cover actual old-policy continuation,
unchanged financial/native choices, lost promotion, transient/revoked access,
closed observation integrity and WordPress/Ghost identity failures. The full
1,378-test website no-database group passes, all 583 modules are assigned and
826 failure emissions are audited offline. The disposable remote ORM flow
also simulates an old saved native choice, asserts the single extra GET and
retains one policy, hold, outbox and publication. Its CI result must be checked.
No local database, customer CMS call, model, merge or deployment runs.

All rollout flags remain off. Reconnect/rebinding, hosted fallback, paid article
revision/unpublish and actual CMS/SEO/nightly acceptance remain prerequisites.
