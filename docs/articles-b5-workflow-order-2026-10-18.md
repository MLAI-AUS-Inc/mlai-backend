# B5 optional timeline order correction

CF declares semantic step order in `RunRecord.step_order`, but the original B5
projection drops it. Canonical JSON sorts the remaining `step_states` keys; the
backend then enumerates those keys into `display_order` and leaves public
`stepOrder` empty. The actual Chat timeline consequently displays alphabetical
steps. This defect reproduces with the real main producer and receiver.

The paired producer reserves optional versioned formatting at
`projection.result.article_system_setup._mlai_workflow_order`. Its canonical
semantics and golden wire examples live in CF `contracts/workflow-order-v1.md`
and `contracts/workflow-order-v1.example.json`. No existing v1 schema, source
lock, queue, receipt or protocol version changes.

The default-false setting
`CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED` admits
`workflow_order_v1 = {"version":1}` only when the backend creates an original
B5 operation, after existing owned B5 enrollment. It is excluded from logical
request identity. Untrusted incoming markers are stripped. Worker request merges
cannot introduce, replace or remove the original marker. Its original presence
or absence persists across retries/resumes; enabling the setting cannot upgrade
historical runs. This cosmetic switch is an observed rollout input, not another
B5 delivery authority or a substitute for an approved fixture cohort.

The receiver requires both backend-saved original requests to admit formatting.
Only a current accepted state can use a unique exact-key-set sequence bounded by
the existing JSON limits. `run.step_order`, row `display_order`, private current
map observation and the processed receipt commit through the existing projection
transaction. Duplicate/stale/observation-only events retain their existing
behavior. Metadata never grants approval or repository/deployment/publication
proof, and article-stream result projection keeps existing rich evidence.

A higher state with absent or invalid optional metadata remains deliverable,
clears stale semantic order, and serializes only rows in that accepted current
map. Older rows remain in storage as history. The reader uses strict private
version/execution/key guards and falls through for malformed legacy JSON, even
when formatting is disabled. Nonmapping saved requests remain rejected by the
pre-existing original-scope guard before formatting. Existing generic transport
limits still reject oversized inputs before the optional parser.

Rollback stops new admission while preserving sticky original choices. Old
receivers accept/ignore the generic subtree; old producers preserve committed
result metadata or, after a new business state, omit it. The new receiver makes
the latter order unavailable. Previously committed bytes replay without rewrite.

Local tests replace only network/ORM/transaction boundaries: actual CF store,
outbox, HTTP adapter, callback/mirror receiver, full backend serializer and real
Chat normalizer/card/timeline. An injected step-write error exercises rollback
through a transaction boundary simulation; real PostgreSQL locking/rollback is
not certified by that test. Default-off typed request, RUN_REQUEST, STATUS and
canonical envelope bytes are compared against main under a fixed clock/root.
No production calls, DB services, migrations, paid providers, flags or customer
operations were used.

Keep this consumer's merge and new admission held until the reviewed compatible
producer is on CF main, its exact release is verified, and the paired gate has
passed. Remote CI, real transaction/volume concurrency and the approved zoo
remain separate release evidence. This correction does not qualify a live
article or complete the one-shot plan.

The explicit database-free delivery/reconciliation/recovery/first-article
selection passes 117 tests, the test-assignment audit reports zero omissions,
and the offline failure-contract audit checks 703 production emission sites.
The producer selection passes 249 tests and its audit checks 1,217 emission sites.
All six actual F1 initializer examples pass the real callback/mirror and full
public serializer; unchanged backend main also accepts/replays them with legacy
display order. Remote pinned provenance and exact final-head CI remain pending
publication. Canonical schema and existing source-lock bytes are unchanged.
