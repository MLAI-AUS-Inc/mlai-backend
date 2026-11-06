# R3 research benefit and terminal settlement

This protocol applies only to explicitly admitted event-owned executions. It
does not enroll historical rows, enable runtime flags, change caps, migrate the
database, or authorize an article, payment, publication or provider write.

Research benefits are distinct from article benefits. A completed Run is not
proof that choices were delivered. An absent article body does not prove that
research delivered nothing.

## Positive choices

`auto_discovery` uses typed evidence `topic_selection_v1`:

```json
{"schema_version":1,"selection":{"options":[{"keyword":"Example topic"}]},"selection_sha256":"<sha256 of selection>"}
```

Brief-led `island_refresh` uses `island_proposals_v1` with the actual measured
`suggested_islands` list and its `proposals_sha256`. Ordinary nightly island
refresh is not converted into a charged topic-research obligation.

CF writes the payload as exact canonical JSON bytes before exposing choices,
and retains evidence on the original creation ticket. Canonical JSON uses
sorted keys, compact separators, ASCII escaping, finite numbers and UTF-8.
The source digest is SHA256 of those exact canonical payload bytes. The pure
authenticated `GET /api/runs/{id}/topic-selection` returns the current echoes
and the corresponding payload.

The backend checks both independently read payload and retained evidence. It
rechecks original admission, execution ticket and current principal before
writing canonical Job selection facts or allowlisted island-result facts. It
does not apply callback status, approval, source, billing or Run-clock fields.
The private `run_owner_topic_delivery_v1` ledger is stripped from public DTOs
and protected from worker snapshots. Verified historical benefit remains
earned after a later failed or cancelled ticket.

The history reconciler also performs this verification after a complete
research observation. A lost topic callback therefore does not lose usable
choices. Callback-supplied choices are never a fallback.

## Notifications

Manual research and island results use the existing founder transition outbox,
recipient resolver, idempotency and delivery transport. The new event contains
generic copy and an Articles link with the company ID. Its current ticket,
verified benefit and recipient permission are checked again before delivery.

An original saved automation context uses the existing channel fan-out. The
automation must belong to the exact organisation and admitted requesting user.
A durable per-benefit claim prevents concurrent/replayed sends. Unknown send
outcomes retain that claim and do not trigger another send. Canonical choices
remain available while notification delivery is uncertain.

## Negative closure

The existing authenticated cancel endpoint may return a separate
`terminal_topic_settlement_v1` receipt. Its closed fields are `schema_version`,
`kind` (`topic_selection_v1` for the TOPIC obligation), `run_id`, the exact
admission and execution echoes, `status` (`closed_no_effects`), `event_cursor`,
`closed_at` and `receipt_sha256`. The digest covers the entire canonical
receipt except its digest field.

CF may seal this proof only after stopping the current execution, fencing every
topic effect and proving the complete monotonic topic-benefit intent history
empty. A prepared topic benefit, unknown provider effect, missing provenance
or in-flight publication prevents closure. Empty completed research is not a
positive benefit by status alone.

The backend retains the exact receipt, drains the immutable global event
history and checks every member of the original charge/grant family. Gaps,
pending tickets, stale echoes, undrained history, saved uncertain choices or
verified benefit hold settlement. Only then may the existing original TOPIC
refund or free-topic-release controller run. The real original payer, amount,
ledger and free-rerun policy remain authoritative.

Both existing topic actions, `content_island_topic_generation` and
`topic_discovery`, identify research. A later separately approved ARTICLE debit
uses its own sealed charge and child identity. A topic benefit cannot keep an
undelivered article charge, and an article refund cannot release the earned
topic grant. Original research request and Run clocks remain unchanged.

Confirmation retains the caller's explicit delivery mode through the existing
article activation and locked charge decision. A fresh original-source,
principal and ticket check runs before the debit. The two existing HTTP
confirmation routes retain the newly admitted article child's logical key;
they do not replace it with the immutable research key on success or replay.

Brief-led island research is currently always priced by its real HTTP producer;
this change does not create a free-island entitlement path.
