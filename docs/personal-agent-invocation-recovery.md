# Personal agent mention recovery

The native agent gateway delivers `agent.mentioned` with an installation ID,
approved resource, invocation ID and signed source event ID inside the MCP event
`data` object. The live OpenAI Dot pilot woke on this webhook but its task did not
receive the invocation ID. A manual handoff verified the real Dot's bounded
thread read, one same-thread reply and accepted native receipt.

`get_invocation` accepts the event's explicit `invocation_id`. If the host omits
it, the Dot may call the same tool without arguments. The gateway returns a
request for the oldest unexpired pending invocation belonging to the
authenticated installation, current grant/generation and approved resources,
with `pending_count` and `more_pending`. Rows with any reply outbox entry are
excluded, even before the reply is sent. With no authorized pending work it
returns `no_pending_invocation` without exposing inaccessible identifiers.
Expired, answered, other-generation, other-resource and other-owner requests do
not participate in selection. Current credentials, owner devices, membership
and `messages:read` authorization are checked before returning any context.

`list_pending_invocations` returns oldest-first work with a limit of 1–20
(default 20), the authorized pending count and a truncation flag. On starting
or resuming a watch, fetch pending work and handle each invocation separately.
The owner can write from several verified devices; `caller_public_key` may
change between mentions and is not a reason for the Dot to refuse a request.
Selection still rechecks every row's device and each distinct caller's current
channel access. Temporary source failures propagate rather than hiding work.
Successful `get_invocation` and `read_invocation_thread` calls record a
content-free fetched audit; a failed read never records delivery.

The returned ID is still required for reading its bounded thread, replying and
checking the receipt. Those operations retain their fixed destination and
one-reply constraints. No permission, channel, grant lifetime or database schema
changes are involved. A fresh real mention must verify automatic recovery after
deployment; a manual diagnostic receipt does not prove automatic handoff.
