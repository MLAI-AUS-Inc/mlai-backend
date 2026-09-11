# MLAI Chat coworking booking

The channel guide’s **Book today** button books the authenticated member directly
through `GET` / `POST /api/v1/community-chat/coworking/today/`. Flutter and
React show a loader during the write and a green **Booked for today** button
only after receiving a confirmed booking receipt. Errors stay retryable.

This is an account API facade over `roo.services.CoworkingService.book`;
capacity, discounts, points debits and per-member/date duplicate protection stay
in the existing Roo domain service. It uses the member’s MLAI points account,
requires a Community Chat account session, and accepts no target identity.
Neither a Slack connection nor a Roo bot/service credential is required. The
existing authenticated account transport enforces native bearer sessions and
browser cookie origin checks. Responses are private and never cached by HTTP.

`GET` returns the member’s existing active booking, or their current points cost:

```json
{
  "date": "2026-09-11",
  "status": "available",
  "booking_id": null,
  "points_cost": 8,
  "resets_at": "2026-09-12T00:00:00+10:00"
}
```

`available` means the member has not booked; capacity is checked atomically
when booking. `POST` sends only `{"date":"2026-09-11"}`. It returns the same
shape with `status: "booked"` and the persisted `booking_id` (201 for creation,
200 for an existing booking). Cost is the actual charged amount. `409` returns
`code` and `detail` for insufficient points, unavailable capacity, or a stale
date. The date is determined in Australia/Melbourne, including daylight saving.
An old date cannot silently book the next day after a timeout at midnight.

Clients read the receipt again when reopening the channel, returning to the
app, or after an uncertain write response. The success state expires at the
server’s next Melbourne midnight. A write failure never becomes green merely
because a message was delivered.

Release the backend endpoint before releasing these clients. No new schema or
migration is needed. No live booking or deployment was performed for this
change. Local validation uses `community_chat.tests.test_coworking_unit`, a
standalone unittest suite with dummy database settings, mocked domain calls and
real DRF dispatch. It does not execute migrations or prove SQL locking/token
validation; those remain covered by the existing integration suites.

## Legacy chat-message handoff

Older clients can still submit the signed message
`@Roo please book me a coworking desk for today.` in
`cowork-and-chill-melbourne` (`b2566a10-c26c-5bae-a362-051254af85ea`).
That path shows “Request sent” and leaves confirmation to Roo. The handoff
feature flag in Home continues to describe this older path.

When there is no valid native `COMMUNITY_CHAT_ROO_PUBLIC_KEY`, the public bridge
worker recognises that exact top-level create command in that exact mapped
channel. Edits, thread replies, other channels and other message text use the
existing bridge behavior. The worker resolves the signed sender through the
existing verified MLAI account/device-to-Slack identity link. A client-supplied
Slack user ID is never accepted.

The worker posts the attributed Slack root, checkpoints its message mapping,
and calls Public Roo's authenticated `/api/mention` with the verified member,
Slack channel, root thread timestamp, `post_reply: true`, and a stable UUID.
The text uses Roo's existing `Please book me in on YYYY-MM-DD.` shortcut, with
the Melbourne date frozen from the durable delivery's creation time. Public Roo
uses the normal coworking workflow and posts its own reply into that thread;
the existing Slack bridge brings that reply back to MLAI Chat.

Retries reuse the stored root and request UUID, without resetting deletion
markers. They retain the original date across midnight. Booking duplication is
also governed by Roo's existing member/date booking intent and backend booking
checks. A Slack reply failure remains retryable; neither service reports it as
delivered. This is not a general agent-creation endpoint.

## Legacy handoff rollout

No new schema, migration, message backfill or production repair is required.
This document describes the code contract, not evidence of a live deployment.

1. Release the Public Roo change that supports `post_reply` and configure its
   `INTERNAL_MENTION_API_KEY`. Keep this credential separate from Admin Roo.
2. Release the backend change, setting `ROO_SERVICE_URL` to that Public Roo
   service and `ROO_INTERNAL_MENTION_API_KEY` to the matching service secret.
   Do not copy secrets into this document, tests or PRs.
3. Ensure the existing public coworking bridge mapping is enabled and the
   member has an unrevoked verified Slack identity link.
4. Release the MLAI Chat clients. `GET community-chat/home/` advertises
   `feature_flags.coworking_booking: true` only when the service configuration,
   enabled mapping and caller's verified Slack identity are present. This flag
   is configuration readiness, not a live Roo health probe. The normal native
   Roo path remains available when its valid public key is configured.
5. After explicit authorization for a live booking, verify that one click
   produces one attributed Slack root and Roo's confirmation in the same chat.

Deployments, configuration changes and real bookings require explicit user
authorization under the repository's AGENTS.md. No live booking was used in
local verification. Removing the backend handoff secret disables capability
advertisement; already queued deliveries can still be inspected normally.

## Legacy handoff verification

`integrations.tests_coworking_handoff` covers configuration and caller identity,
exact command scope, fixed booking dates, service authentication, reply
acknowledgement, root reuse, child wakeup and preservation of deleted links.
Home contract and signed bridge/identity regressions run alongside it using the
approved 334-migration disposable database setup. The unrelated full bridge
worker suite also needs the private Slack delivery table, which is outside
that approved setup; it was not migrated locally for this change.
