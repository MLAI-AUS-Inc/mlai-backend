# MLAI Coding billing contract

MLAI Desktop authenticates the public Coding endpoints with a Community Chat
account access token. The inference gateway authenticates the internal call
accounting endpoints with `X-API-Key: $ROO_API_KEY`. Client-supplied user IDs
are never accepted on the public endpoints.

## Public account endpoints

- `GET /api/v1/community-chat/coding/entitlement/`
- `POST /api/v1/community-chat/coding/turns/`
- `POST /api/v1/community-chat/coding/turns/{turn_id}/ticket/refresh/`
- `POST /api/v1/community-chat/coding/turns/{turn_id}/finalize/`
- `GET /api/v1/community-chat/coding/jwks/`

Entitlement polling and ticket refresh reconcile only the authenticated
account (and, for refresh, the target turn). The scheduled
`reconcile_coding_calls` management command is the only normal global sweep.

## Internal call lifecycle

Every request below carries the same gateway-generated `dispatch_owner`: a
URL-safe random nonce of 32 to 128 characters. Django stores only its SHA-256
digest. The owner prevents a stale handler from dispatching, releasing, or
settling a reservation after an unstarted lease has been taken over.

1. `POST /api/v1/points/kimi/calls/admit/` reserves the conservative call
   maximum. A new reservation returns 201; a replay by the same owner returns
   200 and the same reservation. The response always has
   `dispatch_allowed: false` and `dispatch_start_required: true`.
2. `POST /api/v1/points/kimi/calls/dispatch/` grants provider-dispatch authority
   exactly once. The first response is 201 with `dispatch_allowed: true`. A
   replay is 200 with `dispatch_allowed: false` and must never result in a
   second provider request.
3. `POST /api/v1/points/kimi/calls/settle/` records authoritative usage and
   charges Roo. It rejects calls for which dispatch never started.
4. `POST /api/v1/points/kimi/calls/fail/` releases a definite failure or holds
   a provider-ambiguous failure for reconciliation. A failure reported before
   dispatch-start is always definite and releases immediately.

`MLAI_CODING_DISPATCH_LEASE_SECONDS` controls the unstarted lease and defaults
to 120 seconds (allowed range: 30 to 300). A different owner may recover an
unstarted reservation only after that lease expires. Expired unstarted calls
are released without a 24-hour ambiguity hold. Once dispatch has started,
ownership cannot be transferred.

If the dispatch-start response itself is lost, its replay returns
`dispatch_allowed: false`. This intentionally prefers a safely stranded call
over a duplicate provider request; finalization/reconciliation later releases
or resolves the reservation.
