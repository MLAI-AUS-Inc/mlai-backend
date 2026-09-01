# Roo coworking booking contract

The backend owns coworking capacity, pricing, points charging, and booking
idempotency. Roo calls `POST /api/v1/points/coworking/book/` with the Slack
member ID and requested date.

## Slack account resolution

The booking endpoint resolves identity in this order:

1. Use an MLAI account already linked to the supplied Slack member ID.
2. If none is linked, fetch that exact member's profile from the configured
   MLAI Slack workspace.
3. If the profile contains an email matching an existing MLAI account that is
   not linked to another Slack identity, persist the link and continue the
   booking against that account and its existing Roo Points balance.

The endpoint never creates an MLAI account during booking and never moves an
account from one Slack identity to another. Bot, deleted, mismatched, or
email-less profiles fail closed. A failed Slack lookup returns retryable code
`slack_identity_unavailable`; a verified profile without a safe existing match
returns terminal code `slack_account_not_linked`.

An account created earlier by a Points Admin award is already bound to the
target Slack member ID, even when it uses a placeholder email and the member
has never run `link`. Coworking booking reuses that points owner directly and
charges its existing balance. It does not require email linking or create a
second account.

Roo's self-service `link` command uses the authenticated
`POST /api/v1/users/link-slack/` service endpoint with the email Roo has just
read from the exact Slack member profile. The endpoint accepts only Roo's
dedicated service key, not the broader legacy internal key. Replays are
idempotent. An existing Slack link is authoritative, and an MLAI account
already linked to a different Slack identity returns `slack_identity_conflict`
instead of being reassigned.

Linking is a durable identity update. If linking succeeds but the booking is
rejected (for example, because the account has insufficient points), a later
retry reuses the link. The booking service's existing per-user/date idempotency
key ensures a response-loss retry does not charge the account twice.

The Office Manager variant of coworking booking has a separate authenticated
claim contract, points-refund lifecycle, and Slack delivery runbook. See
[`office-manager.md`](office-manager.md).
