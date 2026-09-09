# Meeting-room booking contract

The Roo-authenticated `/api/v1/points/meeting-rooms/` API owns room availability,
booking confirmation, point debits, owner bookings and cancellations.

The default `rooms/` list contains active `small-meeting-room` and
`big-meeting-room` records only. The distinct `conference-room` resource is
accessed by an explicit room slug and is never included in the default list.

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
