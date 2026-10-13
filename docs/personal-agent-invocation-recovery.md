# Personal agent mention recovery

The native agent gateway delivers `agent.mentioned` with an installation ID,
approved resource, invocation ID and signed source event ID inside the MCP event
`data` object. The live OpenAI Dot pilot woke on this webhook but its task did not
receive the invocation ID. A manual handoff verified the real Dot's bounded
thread read, one same-thread reply and accepted native receipt.

`get_invocation` accepts the event's explicit `invocation_id`. If the host omits
it, the Dot may call the same tool without arguments. The gateway returns a
request only when exactly one unexpired pending invocation belongs to the
authenticated installation, current grant/generation and approved resources.
Zero or multiple matches require an explicit ID; the error exposes no IDs.
Expired, answered, other-generation, other-resource and other-owner requests do
not participate in selection. Current credentials, owner devices, membership
and `messages:read` authorization are checked before returning any context.

The returned ID is still required for reading its bounded thread, replying and
checking the receipt. Those operations retain their fixed destination and
one-reply constraints. No permission, channel, grant lifetime or database schema
changes are involved. A fresh real mention must verify automatic recovery after
deployment; a manual diagnostic receipt does not prove automatic handoff.
