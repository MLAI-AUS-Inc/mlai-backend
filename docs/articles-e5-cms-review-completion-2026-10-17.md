# E5: completion after owner review — 17 October 2026

An owner may choose manual first-article review and return after the seven-day
access/inventory observation window. Previously, even the actual published
article could not finish its setup goal because those earlier proofs expired.

Final native completion now evaluates the original access and inventory
milestones at the actual original draft time, only when that same paid source
also has its exact draft, subsequent private approval and matching fresh public
body proofs for both live milestones. The original proof dates remain visible.
Until actual public verification, those old preparation proofs still expire.
Missing, stale, foreign, misordered or different public subjects do not preserve
them. An already stale policy from before the draft remains stale.

This changes how completed preparation is recorded after an owner wait. It does
not freshen current CMS access, change the source, extend a work budget, issue a
lease or authorize another model/provider request. Ordinary `valid_proof` checks
retain their time limits. Status and the immutable first-publication record use
the same completion semantics, so a completed checklist remains consistent.

The private evidence import in goal authority is now resolved inside the current
scope check. This removes an existing circular import exposed when the native
evidence tests run independently; scope validation itself is unchanged.

Validation includes three pure completion cases with adversarial variants, an
actual mocked native pipeline with old preparation and fresh publication, and
read-only later status retaining the real old timestamps. The full website
no-database suite and other checks are recorded in the PR. No local database,
real CMS call, paid generation, merge or deployment runs. All rollout flags
remain off. Old pre-existing policy access revalidation, reconnect/rebinding,
hosted fallback and real CMS/nightly acceptance remain release prerequisites.
