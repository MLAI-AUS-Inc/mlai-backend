# ADR 0001: One Roo identity with isolated Public and Admin runtimes

- Status: Accepted
- Date: 1 August 2026
- Owners: Security, data, review, and operations owners are recorded in `org_memory/policies/provider_policies.json`

## Context

MLAI already operates Roo as a community-facing Slack bot. The organisational
brain contains committee, executive, finance, email, and source-restricted
evidence. Users should address one `@Roo`, while a routing error or compromised
public process must not be sufficient to retrieve private memory.

## Decision

1. The existing Slack app remains the only Slack identity and ingress.
2. A Public Roo gateway handles Slack verification, normal public skills,
   routing, and final Slack posting. It has no `org_memory.read` credential.
3. An internal-only Admin worker has no Slack credentials or public HTTP
   ingress. It alone holds the `org_memory.read` credential and exposes only
   signed query and feedback dispatch endpoints on a private network.
4. Public Roo holds a distinct `org_memory.route` / `roo_gateway` principal.
   The backend eligibility endpoint is content-free and cannot retrieve memory.
5. Admin eligibility requires all normal backend identity, membership,
   capability, private-context, and active-pilot controls plus an active
   `PointsAdmin` record with the exact `committee` class. No class inherits it.
6. Intent selects the execution surface. Points, Content Factory, Linear, and
   other normal tasks remain Public even for committee callers. Internal
   organisational-memory questions route Admin only after eligibility passes.
7. Requests combining private memory with a public action are split by a
   clarification; no cross-surface chain runs from one prompt.
8. The first Admin release is read-only, source-cited, and live. It has no
   contextual shadow mode, autonomous action, or fallback search path.
9. Published public knowledge remains a separately approved corpus rather
   than a filtered view over private memory.

## Required invariants

- The Slack app credential exists only in Public Roo.
- The memory credential exists only in the internal Admin worker.
- The route-only principal cannot call private-memory endpoints.
- Admin dispatch is HMAC-authenticated, short-lived, single-use, and bound to
  Slack workspace, actor, channel, thread, event, request kind, and payload.
- Admin results are posted only after their returned destination matches the
  original verified Slack destination and requester.
- Public-channel requests never start private retrieval.
- Denials and failures do not fall back to another skill or reveal whether
  inaccessible evidence exists.
- Backend policy, not model output or Roo-supplied roles, makes the final
  access decision.

## Consequences

- MLAI retains one user-facing `@Roo` and one shared codebase, while operating
  two containers and two backend principals with different scopes.
- The public gateway is trusted with Slack message text only after Slack
  verification, but cannot use that text to retrieve memory directly.
- The Admin worker and Public Roo can be disabled and their credentials rotated
  independently. Disabling unified routing restores Public Roo without
  interrupting ingestion or the memory backend.
