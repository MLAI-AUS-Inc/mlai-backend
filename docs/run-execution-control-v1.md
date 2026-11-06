# Execution continuation tickets

New Run admission can include an original `backend_run_budget_v1` request
field, hashed into the immutable admission. It has exactly `schema_version: 1`,
`runtime` (`article` or `maintained_sdk`), a positive decimal string
`max_model_usd`, and a positive integer `max_model_tokens` (at most 20 million).
Only backend configuration creates this field. Worker input cannot set it.

The configuration defaults are `ARTICLES_RUN_CONTROL_MODEL_USD=0`,
`ARTICLES_RUN_CONTROL_MODEL_TOKENS=0`, and
`ARTICLES_RUN_SDK_CONTROL_ENABLED=false`. Zero means no automatic control
budget. Existing admissions retain their original cap across configuration
changes. No historical Run is enrolled or given a new cap. SDK enrollment is
restricted to newly admitted setup/scaffold workflows and explicit backend
configuration. Source changes do not activate these settings in production.

`POST /api/content-factory/runs/{run_id}/execution-ticket` uses existing internal
service authentication. The exact request contains `schema_version: 1`, a
64-character lowercase hexadecimal `control_key`, `reason` (`quality_repair`
or `orphan_resume`), the immutable `backend_run_admission_v1`, and the retained
`backend_run_execution_v1`. There are no body patches, approval decisions,
prices, new principals or worker-supplied permission fields.

The backend checks current requester rights, active status and approval before
remote reads. It reads the worker's conservative budget snapshot and reconciles
one retained history page outside database locks. Moving, incomplete, damaged
or leased history returns a retryable refusal. Inside the final organization
and Run locks it repeats identity, permission and stop checks, verifies the
current ticket has an active event projection, and requires the exact producer
budget cursor to match the completely read history prefix. Repository work
also passes fresh source and operation authority checks. Unknown or exhausted
USD/token counters refuse. Accepted conservative USD/token counters cannot
decrease across grants.

The response contains exactly `schema_version: 1`, the unchanged admission,
and the new execution ticket. A new automatic continuation increments the
generation by one. The private `run_owner_control_v1` ledger retains stable
command keys, previous and issued ticket identities, and budget high water
marks. Retrying an existing command with its original or already issued ticket
acknowledges the same grant. It does not increment the generation or count.
At most 1,024 retained tickets are permitted as a final runaway guard.

An authenticated worker budget read at
`GET /api/runs/{run_id}/execution-budget` contains exactly `schema_version: 1`,
`run_id`, both original/current wire echoes, the original budget field,
`event_cursor`, decimal-string-or-null `used_model_usd_upper_bound`, integer
`used_model_tokens`, and integer `uncertain_calls`. Uncertain provider outcomes
remain conservatively reserved. A missing initialized ledger or unknown price
cannot appear as zero. CF applies the lower of its own limits and the original
backend limits, and retains the budget across every ticket and generation.

An authorized user resume continues through its existing backend controller.
It is not a worker-selected automatic reason. Source and operation controllers
can issue a new scoped ticket after their existing independent proof. The
stored original drafting inputs, admission, approval, original charge and
execution timestamps remain unchanged; current scope is read from the ticket.
The two current backend configuration fields (`configuration_revision` and
`github_installation_id`) are retained in a private map keyed by execution ID
and included in the effective request digest. They add no ticket wire keys.
The private current-scope query projection selects setup candidates; effects
always recheck the actual retained current ticket. The authenticated current
connection endpoint can return a same-generation refreshed ticket after its
existing independent consent checks. Revocation refuses instead of rewriting
the original repository or delivery mode. A copy-only transition remains a
separately authorized control.
Neither kind of ticket grants publication, repository writes or spending by
itself. Those actions retain their original gates.
