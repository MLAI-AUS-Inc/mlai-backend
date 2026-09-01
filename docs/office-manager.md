# Roo Office Manager contract and runbook

The backend owns the Office Manager of the Day state machine, first-volunteer
selection, coworking booking, Roo Points accounting, and durable Slack repair
state. Public Roo only verifies the Slack interaction, persists it in Roo's
action outbox, and calls the claim endpoint.

## Trust boundary

`POST /api/v1/points/coworking/office-manager/claim/` accepts only the
dedicated `ROO_API_KEY`. Send it as `X-API-Key` or `Authorization: Api-Key`.
The broader `INTERNAL_API_KEY` and `MLAI_API_KEY` credentials are rejected
because the caller chooses the Slack member whose booking and points may be
changed.

The request body is:

```json
{
  "slack_user_id": "U0123456789",
  "date": "2026-09-01"
}
```

`slack_user_id` must come from Slack's verified action payload, never from the
button value. `date` is the Melbourne-local date encoded in the backend's
button value. Roo must reject stale dates before calling this endpoint.

## Claim responses

The first accepted claim returns `201` with `status: claimed`. An exact replay
by the winning Slack member returns `200` with
`status: already_claimed_by_you`. Both responses include the authoritative
date, assignment, booking, points-refund amount, and booking provenance. A
committed same-member result is recovered before current-date, feature-flag,
or Slack-profile checks so response-loss retries remain truthful.

Terminal rejections use these codes:

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Missing or malformed Slack ID/date |
| 403 | `member_not_eligible` | Slack member cannot hold the role |
| 404 | `office_manager_day_not_found` | No announcement/day exists |
| 409 | `already_claimed` | Another member won |
| 409 | `claim_closed` | The Melbourne claim window is closed |
| 503 | `feature_disabled` | New claims are disabled |
| 503 | `slack_profile_unavailable` | Slack identity could not be verified yet |

`503` with `code: slack_profile_unavailable`, HTTP `408`, `429`, other `5xx`
responses, transport errors, and malformed success bodies are retryable. Roo
must retry the same Slack member/date payload from its durable outbox. It must
not substitute the current date or another identity. Permission, validation,
conflict, expiry, and insufficient-balance responses are terminal.

## Points, cancellation, and Slack messages

The winning member receives a free coworking booking for that date. If an
existing paid booking is converted, the exact 4- or 8-point charge is refunded
idempotently. Cancelling that converted booking atomically reverses the Office
Manager refund before cancellation; if the member no longer has those points,
the cancellation fails without changing the booking or assignment. Standard
cancellation and refund replays are idempotent.

The backend stores the announcement channel and deterministic Slack message
identifiers with the day/assignment. A relinquished winner's public message is
retracted from durable state. Retraction repair runs even when
`OFFICE_MANAGER_ENABLED=false`, so rollback cannot leave a former winner named
publicly. Public messages may identify the winner, but private booking and
points details belong only in the winner DM and the claim API response.

## Configuration and rollout

Required backend settings are listed in `.env.example`:

- `ROO_API_KEY`: dedicated caller credential shared only with Public Roo.
- `OFFICE_MANAGER_SLACK_BOT_TOKEN`: Public Roo app bot token. The app that posts
  the button must also receive its interaction callback.
- `OFFICE_MANAGER_SLACK_CHANNEL_ID`: coworking channel to announce in.
- `OFFICE_MANAGER_TIMEZONE`, weekday, announcement, cutoff, and reminder
  settings.
- `OFFICE_MANAGER_ENABLED`: backend creation/claim gate, default off.

Roll out in this order:

1. Review and explicitly approve
   `roo.0034_officemanagerday_coworkingbooking_booking_source_and_more` and
   `roo.0035_protect_office_manager_assignment_day`. Deploy and apply them with
   `OFFICE_MANAGER_ENABLED=false`.
2. Configure the dedicated Public Roo Slack token and channel, then run the
   scheduler dry-run/preflight. A missing token or channel must fail closed.
3. Deploy companion Roo PR #210 with `OFFICE_MANAGER_ACTIONS_ENABLED=false`.
4. Smoke-test the exact action ID `office_manager_volunteer_today`, signed
   actor binding, private result delivery, and a duplicate click.
5. Enable the Roo action consumer, then enable the backend scheduler. Monitor
   claim results, pending Slack repairs, and scheduler failures.

To roll back, disable new Roo actions and backend scheduling. Keep the backend
scheduler process running so committed message retractions can continue. Do
not remove either migration or delete Office Manager provenance rows.
