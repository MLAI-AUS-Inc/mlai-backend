# C4: deployment observation and owner waits

14 October 2026. Continuous implementation authorisation covers this design and
code; rollout remains independently disabled.

Read-only hosting signals identify Vercel, Netlify, Cloudflare Pages/Workers and
GitHub Pages/Actions. Provider identification alone does not prove a connected
automatic deploy: config paths and CDN headers identify hosting, while an Actions
workflow triggered by a push to the selected default branch can prove automation.
Explicit manual deployment configuration asks the owner once; unknown automation
gets the existing bounded propagation wait before one provider-specific action.
Public headers and repository config never grant publish/merge/readiness authority.

Saved observations bind the current connection generation and source, retain only
provider/signal/confidence/automation values, and discard raw headers, URLs and
workflow bodies. After every remote observation, the existing authority fence is
checked before saving. Live continuation still needs the existing exact artifact
marker and source proof. A manual prompt retains its quiet observer so the marker
can resume the goal without another owner click. A11 still decides merge authority
and B6's merged-PR webhook resumes the same goal.

Required owner waits pause only the goal wall timer. Compute, provider waits, USD
and attempt budgets continue to count. The ledger records one open wait start and
cumulative closed duration; replay cannot double-count. Invalid/future timestamps
or impossible durations fail closed. Enter/exit is saved with the normal goal lease.

Tests cover each provider signal, default-branch Actions automation, ambiguous or
foreign-root evidence, safe projection, one stable manual action, marker-driven
resume, and replay/exact wall-budget edges across required owner waits.
