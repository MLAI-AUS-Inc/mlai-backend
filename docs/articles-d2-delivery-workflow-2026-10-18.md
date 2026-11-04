# D2: enroll publication children under their actual worker workflow

A newly reserved `article_publish` or `publish_article` operation with a
`source_run_id` was enrolled as `confirmed_topic`. Its worker actually reports
`direct_generate`, as the existing delivery validator already expects. A valid
publication outcome could therefore fail delivery binding and leave the goal
waiting. Explicit `direct_generate` and `confirmed_topic` names had the same
source-presence inconsistency.

Enrollment and validation now use one workflow mapping. Generation still maps
to `confirmed_topic` only when it has a source run. Publication always maps to
`direct_generate`. Existing enrolled streams retain their saved workflow;
this change does not reinterpret historical delivery evidence or change goal,
source, tenant and rollout checks.

Four truth-table cases failed before the fix. Afterward, 115 strict database-free
checks pass across delivery enrollment/receiver, first-article recovery and
publication guards. Database and network access are forbidden by that runner.
No models, migrations or rollout defaults change.

This is a D2 prerequisite, not stale-proposal refresh completion. A new refresh
still needs its own backend reservation, worker preparation and fresh publication
proofs. No old approval or operation gains authority from this mapping.
