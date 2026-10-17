# E5: original artifact reads remain separate from model execution

The service-only `channel_artifact_read` action reads the exact original paid
CMS artifact without admitting another model call or provider POST. It rechecks
the original outbox/envelope, selected owner/site/policy/key and actual original
payer/spend/refund state. A missing run, cancelled source or real credit denies
the read. The response remains the closed minimal envelope with no capabilities,
provider key or financial details.

The existing `channel_artifact` action still requires the current active native
goal and its budget for model work. A stopped or goal-rollout-disabled run can
therefore retain its original handoff for GET-only recovery of an already issued
CMS resource, while new work stays denied. Incoming `for_execution`/`for_read`
fields cannot select this authority boundary; the authenticated service route
and private CF reader select it in code.

CF's handoff reader must use the paired `channel_artifact_read` action before
and after collecting the original handoff. Model workers keep execution admission.
Deploy both implementations before enabling native/CMS rollout. A read receipt
cannot be reused as model or publishing authority.

All 1,196 no-database website checks pass, with stopped/disabled-goal reads and
actual credit/owner denial coverage. The failure audit covers 804 emissions and
all 569 test modules are assigned. The disposable ORM fixture adds an original
held-spend read while native execution is disabled; remote execution is pending
CI. No migration/model change, actual service/provider call, paid model work,
local database, merge or deployment ran. All rollout flags remain off.
