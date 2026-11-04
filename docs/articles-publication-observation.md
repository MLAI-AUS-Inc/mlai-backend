# Bounded positive publication observation

This backend change is layered on compatible articles integration #1148 and pairs with Content Factory's publication-intent writer. Deploy the backend receiver before enabling the paired CF writer: Git push requires acknowledgement of the exact intent ID. Existing historical journals are not automatically enrolled.

The authenticated mutation endpoint validates a typed article intent against the original durable run and exact file ledger, then creates an idempotent publication-observe operation in the existing lifecycle outbox. The intent contains identifiers and hashes only. The original charge, goal, source, repository, execution attempt and deletion epoch remain bound; generic setup mutations cannot enroll it.

The scheduler waits for a validated stopped, uncertain article before reading GitHub. It checks the current selected connection and original operation attempt, then performs bounded authenticated GETs outside database locks. The exact repository ID, branch ref, commit, tree and source parent must match the pre-push intent. This first version supports the originally consented repository only; forks require a separately reviewed authority path.

After provider reads and after the private CF acknowledgement, the backend rechecks connection consent, original scope and the saved worker facts under the established organization → connection → operation → run locks. Newer publication or cancellation evidence wins over an older in-flight observation. Response bodies are capped at 64 KiB and redirects are refused.

A confirmed observation records positive article evidence and wakes an existing parent settlement without resetting its budgets. It does not stop/restart work, publish, close PRs, delete branches, revoke the owner's connection, refund or create a charge. The original cancellation receipt remains unchanged. Missing refs, branch movement, missing retained intents and transport failures remain unknown. Three attempts or a 24-hour deadline retire observation work to needs_attention while preserving reconciliation and financial obligations.

The actual CF writer/serializer/store fixture is consumed by backend unit and PostgreSQL transaction tests. CI covers registration replay, original-attempt changes, selected-connection changes and a newer callback arriving during the acknowledgement. Database tests are assigned to disposable CI; local validation uses the repository's database/network-denying runner.
