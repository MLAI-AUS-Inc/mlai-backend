# MLAI Chat ↔ Slack staging acceptance

This is the live, operator-run acceptance procedure for one selected,
non-sensitive public Slack channel. It complements the automated bridge tests;
it does not put Slack installation or mapping controls in the MLAI Chat member
UI.

## Safety and evidence rules

- Use dedicated staging Slack and MLAI communities and two human test accounts.
- Install the bridge bot only in the approved public test channel. Confirm it
  is not private, a DM, or Slack Connect.
- Store secrets only in the backend/adapter secret manager. Clients receive
  only the bridge public key.
- Use synthetic content. Evidence must contain timestamps, source IDs,
  destination IDs, delivery IDs, statuses, image digests, and tester names—not
  message bodies, tokens, keys, email addresses, or invite URLs.
- Start with the mapping disabled. Record the schema version and immutable
  backend, relay, adapter, browser, desktop, iOS, and Android artifact IDs.

## Preconditions

1. Verify the Slack request signature and callback replay-window checks are
   enabled, the worker and adapter health checks pass, and the dedicated bridge
   signer is not a human member key.
2. Confirm `MLAI_BRIDGE_PUBKEY` is the public half of that signer in all client
   artifacts. Run the MLAI Chat release and client-boundary contract scripts.
3. Create the mapping with `upsert_community_bridge_channel`; inspect it in the
   admin and confirm edit, delete, and reply synchronization are enabled.
4. Verify one human identity link and leave the second Slack user deliberately
   unlinked. Record only the link row/audit reference.
5. Start structured-log capture for receipt key, delivery ID, attempts, source
   and destination IDs, outcome, and latency. Message text must not appear.

## Bidirectional message matrix

Perform every row in both a browser and one signed desktop/mobile client. A
different tester observes the destination before the source tester continues.

| Source | Action | Expected destination result |
| --- | --- | --- |
| Slack | Create a root message | Appears exactly once in the normal MLAI Chat channel UI with `via Slack` |
| Slack | Reply in its thread | Appears once under the mapped MLAI parent |
| Slack | Edit the root | Same mapped MLAI object changes; immutable provenance remains |
| Slack | Add/remove each approved reaction | Exact mapped reaction appears/disappears; parent remains |
| Slack | Add a custom reaction | Ignored, with no delivery |
| Slack | Delete the reply and root | Exact mapped MLAI objects are removed |
| MLAI Chat | Create a root message | Appears exactly once in Slack, attributed to MLAI Chat |
| MLAI Chat | Reply in its thread | Appears once under the mapped Slack parent |
| MLAI Chat | Edit the root | Same mapped Slack message changes |
| MLAI Chat | Add/remove each approved reaction | Slack reaction API changes the exact mapped reaction |
| MLAI Chat | Add an unsupported reaction | Fails closed and is not mirrored |
| MLAI Chat | Delete the reply and root | Exact mapped Slack messages are removed |

For Slack-origin events, confirm linked-user messages expose the verified
linked public key and unlinked-user messages do not. In both cases the event
signer must remain the dedicated bridge key; the UI must never imply that the
human signed it.

## Retry, dead-letter, echo, and mapping controls

1. Replay the same Slack `event_id`; confirm one receipt and one destination
   object. Retry one claimed adapter delivery; confirm its Nostr event ID is
   unchanged. Retry one MLAI-to-Slack create; confirm deterministic
   `client_msg_id` prevents a duplicate.
2. Stop the adapter, create a synthetic message, and let a delivery enter
   `failed`. Restart it and confirm completion with increasing attempts and one
   destination object.
3. In staging only, force a delivery to exhaust its attempts. Investigate the
   recorded error, then run `requeue_community_bridge_delivery <id> --confirm`.
   Confirm the same delivery ID completes once.
4. Confirm events signed/authored by the bridge key and events authored by the
   Slack bridge bot do not enqueue a reverse delivery.
5. Disable the mapping. Create one message on each side and confirm both are
   ignored. Re-enable it; new messages synchronize while disabled-period
   messages are not backfilled.
6. Attempt edit/delete/reaction removal with a foreign Nostr signer. Confirm the
   adapter rejects it and the origin-owned object remains unchanged.

## Durable verification and approval

After creating one fresh root message in each direction, run:

```sh
python manage.py verify_community_bridge_staging \
  --slack-channel-id <C...> \
  --slack-message-id <Slack ts> \
  --buzz-event-id <64-character event ID> \
  --slack-reaction-id <reaction:...> \
  --buzz-reaction-event-id <64-character reaction event ID>
```

Reaction arguments are optional, but required for release approval. Attach the
content-free JSON result and screenshots from both clients to the release
record. Approval requires zero dead deliveries, no duplicates/echoes, complete
link mappings, correct verified/unverified attribution, and sign-off from the
backend, client, security, and community operations owners.
