# MLAI Chat coworking handoff

The channel guide's **Book today** button submits the normal signed chat message
`@Roo please book me a coworking desk for today.` in
`cowork-and-chill-melbourne` (`b2566a10-c26c-5bae-a362-051254af85ea`).
It requests a booking; the client shows “Request sent”, with Roo responsible for
confirming availability and cost. It does not claim a confirmed booking on send.

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

## Coordinated rollout

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

## Local verification

`integrations.tests_coworking_handoff` covers configuration and caller identity,
exact command scope, fixed booking dates, service authentication, reply
acknowledgement, root reuse, child wakeup and preservation of deleted links.
Home contract and signed bridge/identity regressions run alongside it using the
approved 334-migration disposable database setup. The unrelated full bridge
worker suite also needs the private Slack delivery table, which is outside
that approved setup; it was not migrated locally for this change.
