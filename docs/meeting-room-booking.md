# Meeting-room booking contract

The Roo-authenticated `/api/v1/points/meeting-rooms/` API owns room availability,
booking confirmation, point debits, owner bookings and cancellations.

## MLAI Chat account API

MLAI Chat clients must use the account-authenticated facade at
`/api/v1/community-chat/meeting-rooms/`;
It binds every request to the signed-in Chat account and calls the same
Roo service, so availability, point debits, idempotency and cancellations have
one source of truth.
It does not use Roo API Key. Intended to be used for UIs that are running outside
of Roo Context.

Todo - I think we need a generalised strategy for agent + api access, rather than a patchwork
of adapters.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `rooms/` | List the default active rooms. |
| `POST` | `availability/` | Check a room for a date or exact interval. |
| `POST` | `book/` | Confirm an account owner's booking. |
| `GET` | `my-bookings/` | List the account owner's active future bookings. |
| `POST` | `cancel/` | Cancel the account owner's booking. |

The facade accepts the same room slug, timestamp, confirmation and expected
price fields as the Roo API, except it derives the member identity from the
account session and does not permit administrative target-member bookings.

The default `rooms/` list contains active **Lovelace** (`small-meeting-room`)
and **Turing** (`big-meeting-room`) records only. The distinct `conference-room`
resource is accessed by an explicit room slug and is never included in the default list.

## Booking duration

New bookings must be exactly **1 or 2 elapsed hours**, costing 1 or 2 Roo Points.
Starts and ends can still be on the hour or half-hour. Ninety-minute bookings,
including old previews confirmed after rollout, are rejected before any debit
or reservation creation. Availability can inspect broader intervals, but marks
anything other than a one- or two-hour interval `bookable: false` with no price.
The configured maximum can narrow this policy, not permit fractional or longer
bookings.

Already confirmed 90-minute bookings remain valid, visible and cancellable.
Replaying their completed requests returns the original result without another
charge. No schema or data migration is required. Deploy the backend guard before
the companion Roo change for enforcement on stale confirmations and direct API
requests; merging this backend PR triggers the existing backend deployment.

## Conference Room access

The booking member must have **strictly more than 100 lifetime-earned Roo
Points**. Exactly 100 is ineligible. The authoritative microroo value preserves
fractions (100.000001 qualifies); legacy accounts use their lifetime-earned
projection until initialized. Purchased top-ups do not qualify, and spending
points does not reduce lifetime contribution. Booking still requires enough
spendable points for the ordinary meeting-room charge.

Availability checks eligibility before returning room or busy-time details.
New bookings check eligibility and recheck under the points-account lock before
charging or creating a reservation. Full Points Admins can book for someone
else, but eligibility and charges belong to the target member. There is no
admin eligibility bypass.

Denied access, an absent Conference Room record, and an inactive Conference Room
return HTTP 409 with only:

```json
{"code":"room_unavailable","error":"The Conference Room is unavailable."}
```

Roo renders that message privately without explaining the rule. Successful
availability and confirmation preserve the existing response shape. A replay
of an already completed booking returns the existing result without another
charge, even if eligibility subsequently changes. Members can still view and
cancel their own existing reservations under the ordinary cancellation rules.

## Room configuration and release

No schema migration is needed. The existing Django **Meeting rooms** admin can
create the new record using name **Conference Room**, slug **conference-room**,
and **is active** enabled. The slug identifies the protected resource and must
remain `conference-room`.

Deploy the backend access checks **before creating or activating this room**,
then deploy the companion Roo support. Do not configure it against an older
backend, which does not enforce this eligibility rule. Creating the row and
production deployment are separate operational steps; opening the PR does not
perform either. No new migration, startup seeding or production mutation is
included in this change.

## Tests

`roo.tests_meeting_rooms.ConferenceRoomApiTests` covers threshold boundaries,
precision and legacy accounts, purchased points, missing accounts, normal
pricing, admin target identity, repeated access checks, private denials, default
list filtering, inactive/missing rooms, replay and cancellation. It runs in the
existing meeting-room CI selection. The existing room API regressions protect
authentication, conflicts, refunds, daily limits and signature/request binding.

## Lovelace and Turing rename

Migration `roo.0042_rename_meeting_rooms` changes the existing Small Meeting
Room name to **Lovelace**, and Big Meeting Room name to **Turing**. It updates
only the name on each existing slug. Room IDs, active status, bookings, blocks,
prices, and API request fields remain unchanged. Missing rooms are not created.
The reverse migration restores the original display names.

Roo recognizes the new names without requiring the word `room`, and continues
to accept the old names and size-based aliases. Keep the stable API slugs
`small-meeting-room` and `big-meeting-room` in integrations and stored callbacks.

Apply the separately approved production migration with the backend release
before releasing the companion Roo change, so all backend-provided availability,
confirmation, booking-list and cancellation labels use the new names. Historical
Slack messages are not rewritten; their legacy action payloads still work.
Production deployment and migration application are separate authorized actions.
