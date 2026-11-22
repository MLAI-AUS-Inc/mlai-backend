# MLAI Arcade

Publishing a Pulse update for the first time earns its publisher a pull, regardless
of the update's audience. A saved draft, a republished edit or a teammate viewing
the same update does not earn another pull. Each newly crossed level on the
member-facing Roo ladder earns another pull, including when one contribution
crosses several thresholds. Wallet prizes do not count toward this ladder.

## Account API

Use the MLAI Chat account session. All responses are private and uncached.

| Method | Path under `/api/v1/community-chat/` | Result |
| --- | --- | --- |
| GET | `arcade/rounds/` | Oldest 50 unacknowledged rounds belonging to this member |
| POST | `arcade/rounds/{id}/spin/` | Reveals the round, or returns the previously committed result |
| POST | `arcade/rounds/{id}/acknowledge/` | Dismisses a revealed round (204); an unspun round returns 409 |

The list is wrapped in `{"rounds": [...]}`. Each round has `id`, `trigger`
(`pulse` or `level`), optional `level` and nullable `prize`. A spin returns the
round object directly. Clients submit no draw or reward parameters. They can
display the same result after an interruption and acknowledge it after the member
finishes. A subsequent list call returns the next page when earlier rounds are
acknowledged. Cross-account UUIDs return 404.

| Draw | Initial chance | What the member receives |
| --- | --- | --- |
| `roo_points` | 7% | 10 Roo points, credited to the member's wallet. |
| `roo_points` | 19% | 5 Roo points, credited to the member's wallet. |
| `roo_points` | 30% | 1 Roo point, credited to the member's wallet. |
| `event_ticket` | 8% | One use of a stocked half-price ticket code for an MLAI event. The payload includes `eventName`, `eventUrl`, and `ticketCode`. |
| `codex_credits` | 1% | A $50 Codex credit code, emailed to the winner's verified account address. The API never returns a Codex code. |
| `none` | 35% | No prize. |

The points payload includes the awarded amount in `points`. An unavailable
ticket or Codex draw pays 5 Roo points. Codex eligibility also
requires an eligible recipient, configured sender, available stock and at least
36 hours since the previous Codex award. At capacity the first and twentieth
awards fall at least 28½ days apart. Lower activity stretches the stock further.
The locked stock reservation and persisted outcome commit together. Duplicate
pulls across devices return that same award.

Staff import stock and monitor delivery in MLAI Admin CRM. See
[CRM prize stock and email operations](crm.md#mlai-arcade-prize-stock-and-email).
The durable round is a scoped Volunteer source receipt and the points
award uses the existing Roo ledger. The feature requires no database migration.
