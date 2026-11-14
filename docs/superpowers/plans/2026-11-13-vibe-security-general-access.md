# Vibe Security general access completion plan

> Implement inline with superpowers:executing-plans; preserve existing release worktrees.

**Goal:** All eligible MLAI company owners can onboard without manual company enrollment.

**Architecture:** Explicit production general-access configuration replaces the pilot list only when enabled. Existing MLAI session, device, community, company ownership and organization checks remain mandatory. After those checks, reserve one durable workspace UUID per company using the existing binding table. A verified GitHub callback provisions that UUID in the private history service; retries return the same workspace, and another GitHub owner cannot claim an existing one.

**Scope:** Website only; no pricing, model, scan or mobile changes. Existing inactive bindings remain revoked. No new Django migration. History needs a separately reviewed additive SQL function before enabling general access; do not silently apply it to production.

**Approach:** Keep existing company bindings and GitHub-scoped history authorization. Merely deleting the allowlist leaves new users blocked. Replacing history tenancy or granting all GitHub accounts membership is unnecessary and unsafe.

## Tasks

- [ ] Add failing configuration/authorization/binding tests, then implement explicit general access and idempotent company binding. Test foreign companies, revoked sessions and inactive bindings.
- [ ] Add failing GitHub callback and private history provisioning tests. Preserve the server-authenticated provisioning claim through login and introspection, never accept it from browser input. Provision only at GitHub callback, then use normal history membership checks.
- [ ] Add history 002 function: bounded UUID/name inputs, transaction-safe creation with caller ownership, existing foreign workspace denied, restricted function execution. Verify with a fresh disposable PostgreSQL database and no external calls.
- [ ] Complete browser CI fixture corrections and minimal Hickory security patch. Keep production localhost redirect restrictions; test both canonical and dedicated integration configurations.
- [ ] Run focused suites and review cross-tenant/retry/rollback boundaries; update docs, commit and push PR updates. Keep review requirements intact.
- [ ] Verify required CI; coordinate additive history function rollout before enabling general access. Verify new-user onboarding without paid scans or charges.

## Rollback

Disable general access and restore the previous pilot company list. Preserve bindings, history and receipts. Never reactivate a revoked binding or delete a workspace to recover a failed onboarding attempt.
